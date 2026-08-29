"""any2bsky terminal runner (modern subcommand style).

Commands (pipeline steps):
    sources                 list registered datasources
    login                   interactive login; caches the session (data/session.json)
    convert <root>          datasource -> data/<src>/events.json
    plan <root>             events -> data/<src>/tasks.json (+ compressed/)
    dry <root>              dry-run the DAG executor on a tasks.dry.json copy
    live <root>             REAL posting (reuses the cached session only)

All artifacts live under ./data (shared.paths); the source export directory
is never written to. Datasources are registered at import time
(datasource/__init__.py); pick one with --source (default: qzone).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import shutil
import sys
import time
import webbrowser
from collections.abc import Sequence

from datasource import available, get_datasource
from datasource.base import BaseDataSource
from shared.auth import AuthError, login_client
from shared.event import load_events
from shared.executor import run_live, run_tasks_file
from shared.filter_server import FilterServer
from shared.paths import dry_tasks_path, events_path, tasks_path
from shared.planner import load_tasks, plan_events, write_tasks
from shared.undoer import plan_undo, run_undo


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_sources(_args: argparse.Namespace) -> int:
    print("datasources:", ", ".join(available()))
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    """Interactive login (password hidden via getpass); caches the session."""
    handle = args.handle
    password = args.password
    for attempt in range(1, 4):
        try:
            handle = handle or input("Bluesky handle: ").strip()
        except EOFError:
            print(
                "[login] aborted: no handle (pass --handle in non-interactive shells)",
                file=sys.stderr,
            )
            return 1
        try:
            password = password or getpass.getpass("App password (hidden): ")
        except (EOFError, KeyboardInterrupt):
            print(
                "[login] aborted: interactive input unavailable; "
                "call with --handle/--password",
                file=sys.stderr,
            )
            return 1
        try:
            client = asyncio.run(login_client(handle, password, args.session))
        except AuthError as e:
            print(f"[login] failed ({attempt}/3): {e}", file=sys.stderr)
            handle = password = None
            continue
        except Exception as e:  # noqa: BLE001
            print(
                f"[login] failed ({attempt}/3): {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            return 1
        me = client.me
        print(f"[login] ok: {me.handle} ({me.did}) -> session cached")
        return 0
    return 1


def cmd_convert(args: argparse.Namespace) -> int:
    ds: BaseDataSource = get_datasource(args.source)
    out = ds.convert(args.root)
    print(f"[convert] {out}")
    return 0


def _events_media_root(events_json: str, fallback: str) -> str:
    """Media paths in events are relative to the SOURCE export root; prefer
    events.json source.root over the CLI root (which may be a data/ dir)."""
    try:
        with open(events_json, "r", encoding="utf-8") as f:
            doc = json.load(f)
        src = (doc.get("source") or {}).get("root")
        if src and os.path.isdir(src):
            return os.path.abspath(src)
    except (OSError, json.JSONDecodeError):
        pass
    return fallback


def cmd_plan(args: argparse.Namespace) -> int:
    """Plan from the CONVERTED events.json (not a fresh datasource re-parse),
    so manual filtering (tools/editor.html, {"drop": true}) is honored."""
    src = events_path(args.root)
    if not os.path.exists(src):
        print(
            f"[plan] aborted: no events.json; run `convert` first ({src} missing)",
            file=sys.stderr,
        )
        return 1
    events, dropped = load_events(src)
    media_root = _events_media_root(src, os.path.abspath(args.root))
    tasks = plan_events(events, media_root)
    out = tasks_path(args.root)
    write_tasks(tasks, out)
    failed = sum(1 for t in tasks if t.state == "failed")
    print(
        f"[plan] {len(events)} events (+{dropped} dropped) -> {len(tasks)} tasks -> {out} "
        f"(failed={failed})"
    )
    return 0


def cmd_dry(args: argparse.Namespace) -> int:
    src, dst = tasks_path(args.root), dry_tasks_path(args.root)
    if not os.path.exists(src):
        print(f"[dry] aborted: run `plan` first ({src} missing)", file=sys.stderr)
        return 1
    shutil.copy(src, dst)
    stats = run_tasks_file(dst, heavy_concurrency=args.heavy)
    print(f"[dry] {stats.to_dict()} -> {dst}")
    return 0


def cmd_filter(args: argparse.Namespace) -> int:
    """Local mini-server + editor: manually keep/drop events (backend applies)."""
    try:
        srv = FilterServer(args.root, port=args.port)
    except FileNotFoundError as e:
        print(f"[filter] aborted: {e}", file=sys.stderr)
        return 1
    url = srv.start()
    print(f"[filter] {url}", flush=True)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001, S110  browser unavailable; server still runs
        pass
    print("[filter] press Ctrl+C to stop the server", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
        print("\n[filter] stopped")
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    """后悔药: delete published posts recorded in tasks.json (child-first)."""
    src = tasks_path(args.root)
    if not os.path.exists(src):
        print(
            f"[undo] aborted: no tasks.json; run `plan` first ({src} missing)",
            file=sys.stderr,
        )
        return 1
    tasks = load_tasks(src)
    _order, stats = plan_undo(tasks)

    if args.dry:
        print(f"[undo/--dry] 将删除 {stats.eligible} 条（skipped={stats.skipped}）:")
        for i, t in enumerate(_order, 1):
            print(f"  {i:>3}. {t.post_uri}  {t.text[:40]!r}")
        return 0
    if stats.eligible == 0:
        print("[undo] 没有可撤销的已发任务（state=done 且有 post_uri）")
        return 0
    if not args.yes:
        try:
            ans = (
                input(f"[undo] 将删除 {stats.eligible} 条已发帖子，确认? [y/N] ")
                .strip()
                .lower()
            )
        except EOFError:
            print("[undo] 非交互环境: 请加 --yes", file=sys.stderr)
            return 1
        if ans not in ("y", "yes"):
            print("[undo] 已取消")
            return 0
    try:
        out = run_undo(src, session_path=args.session)
    except Exception as e:  # noqa: BLE001  AuthError + network errors
        print(f"[undo] failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"[undo] {out.to_dict()}")
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    src = tasks_path(args.root)
    if not os.path.exists(src):
        print(f"[live] aborted: run `plan` first ({src} missing)", file=sys.stderr)
        return 1
    try:
        stats = run_live(src, heavy_concurrency=args.heavy, session_path=args.session)
    except AuthError as e:
        print(f"[live] failed: {e}", file=sys.stderr)
        print(
            "[hint] run `any2bsky login` first (session cache: data/session.json)",
            file=sys.stderr,
        )
        return 1
    except Exception as e:  # noqa: BLE001  network / upload / record errors
        print(f"[live] failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"[live] {stats.to_dict()}")
    return 0


# --------------------------------------------------------------------------- #
# Argument tree (subcommands, no boolean switches)
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="any2bsky",
        description="terminal runner: convert -> plan -> dry/live",
    )
    sub = ap.add_subparsers(dest="command", required=True, metavar="<command>")

    def source_opt(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--source",
            default="qzone",
            help=f"datasource key ({', '.join(available())}, default: qzone)",
        )

    def heavy_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--heavy",
            type=int,
            default=2,
            help="heavy (media) task concurrency (default: 2)",
        )

    p = sub.add_parser("sources", help="list registered datasources")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("login", help="interactive login, cache the session")
    p.add_argument("--handle", help="skip prompt; fallback env BSKY_HANDLE")
    p.add_argument("--password", help="skip prompt; fallback env BSKY_APP_PASSWORD")
    p.add_argument("--session", help="session cache file (default data/session.json)")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("convert", help="datasource -> data/<src>/events.json")
    p.add_argument("root")
    source_opt(p)
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser(
        "plan", help="events.json -> data/<src>/tasks.json (+ compressed/)"
    )
    p.add_argument("root")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("filter", help="mini-server + browser editor: keep/drop events")
    p.add_argument("root")
    p.add_argument("--port", type=int, default=0, help="port (0 = auto pick)")
    p.set_defaults(func=cmd_filter)

    p = sub.add_parser("dry", help="dry-run the DAG executor on a tasks.dry.json copy")
    p.add_argument("root")
    heavy_opts(p)
    p.set_defaults(func=cmd_dry)

    p = sub.add_parser("undo", help="后悔药: 删除 tasks.json 里已发布的帖子")
    p.add_argument("root")
    p.add_argument(
        "--dry", action="store_true", help="只列出将删除的帖子，不登录不删除"
    )
    p.add_argument("--yes", action="store_true", help="跳过交互确认（非交互环境必须）")
    p.add_argument("--session", help="session cache file (default data/session.json)")
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("live", help="REAL posting (reuses the cached session)")
    p.add_argument("root")
    heavy_opts(p)
    p.add_argument("--session", help="session cache file (default data/session.json)")
    p.set_defaults(func=cmd_live)

    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
