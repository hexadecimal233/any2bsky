"""Undo / rollback: delete posts that were published from a tasks.json.

"后悔药" -- a safety net for `live` postings:

- only posts whose post_uri belongs to the CURRENT account are deleted
  (uri did must match the session did); anything else is skipped
- reply-chain posts are deleted CHILD-FIRST (deepest chain tail first)
- after a successful delete the task is reset to state=pending with
  post_uri/post_cid/parent_uri cleared, so it can be re-planned/re-posted
- failed deletes keep their uri and are marked failed for a retry
- checkpoint (tasks.json) is rewritten after every delete

--dry lists the posts that WOULD be deleted without touching anything and
without logging in.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass

from atproto import models as ap_models

from shared.auth import login_client
from shared.planner import (
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
    Task,
    load_tasks,
    write_tasks,
)


class UndoError(Exception):
    """Raised for invalid task arrays or unsupported URIs."""


@dataclass
class UndoStats:
    eligible: int = 0  # state=done with a post_uri
    deleted: int = 0
    failed: int = 0
    skipped: int = 0  # uri not owned by the current account

    def to_dict(self) -> dict[str, int]:
        return {
            "eligible": self.eligible,
            "deleted": self.deleted,
            "failed": self.failed,
            "skipped": self.skipped,
        }


def _parse_post_uri(uri: str) -> tuple[str, str, str] | None:
    """Split an at:// uri into (did, collection, rkey); None if malformed."""
    parts = uri.split("/")
    if len(parts) == 5 and parts[0] == "at:" and parts[1] == "":
        return parts[2], parts[3], parts[4]
    return None


def _child_first_order(tasks: list[Task]) -> list[Task]:
    """Order tasks so reply children are deleted before their parents."""
    by_id = {t.id: t for t in tasks}

    def depth(t: Task) -> int:
        if t.reply_to is None or t.reply_to not in by_id:
            return 0
        return depth(by_id[t.reply_to]) + 1

    return sorted(tasks, key=lambda t: -depth(t))


def _eligible(tasks: list[Task]) -> list[Task]:
    return [t for t in tasks if t.state == STATE_DONE and t.post_uri]


def plan_undo(tasks: list[Task]) -> tuple[list[Task], UndoStats]:
    """Which posts WOULD be attempted (dry listing), without any network access.

    Ownership cannot be checked here (needs the session did), so
    "skipped" only counts malformed uris at this stage; non-owned posts are
    skipped at execution time.
    """
    stats = UndoStats()
    order: list[Task] = []
    for t in _child_first_order(tasks):
        if t.state != STATE_DONE or not t.post_uri:
            continue
        stats.eligible += 1
        parsed = _parse_post_uri(t.post_uri)
        if parsed is None:
            stats.skipped += 1
            continue
        order.append(t)
    return order, stats


async def _delete_one(client, task: Task, me_did: str) -> None:
    parsed = _parse_post_uri(task.post_uri or "")
    if parsed is None:
        raise UndoError(f"malformed post_uri: {task.post_uri}")
    did, collection, rkey = parsed
    if did != me_did:
        raise UndoError(f"not owned by this account: {task.post_uri}")
    if collection != "app.bsky.feed.post":
        raise UndoError(f"unexpected collection: {task.post_uri}")
    await client.com.atproto.repo.delete_record(
        ap_models.ComAtprotoRepoDeleteRecord.Data(
            repo=me_did, collection=collection, rkey=rkey
        )
    )
    task.post_uri = None
    task.post_cid = None
    task.parent_uri = None
    task.fail_reason = None
    task.state = STATE_PENDING  # can be re-planned / re-posted


async def _run_undo(path: str, session_path: str | None):
    tasks = load_tasks(path)
    order, stats = plan_undo(tasks)
    if not order:
        return stats
    client = await login_client(session_path=session_path)
    me_did = client.me.did
    last_report = 0.0

    def report(force: bool = False) -> None:
        nonlocal last_report
        now = time.monotonic()
        if not force and now - last_report < 1.0:
            return
        last_report = now
        print(
            f"\r[undo] {stats.deleted + stats.failed + stats.skipped}/{len(order)} "
            f"deleted={stats.deleted} failed={stats.failed} skipped={stats.skipped}   ",
            file=sys.stderr,
            end="",
            flush=True,
        )

    for t in order:
        try:
            await _delete_one(client, t, me_did)
            stats.deleted += 1
        except UndoError:
            stats.skipped += 1  # not ours / malformed: leave untouched
        except Exception as e:  # noqa: BLE001  keep uri for retry
            t.state = STATE_FAILED
            t.fail_reason = f"undo: {type(e).__name__}: {e}"
            stats.failed += 1
        write_tasks(tasks, path)
        report()
    report(force=True)
    print(file=sys.stderr)
    return stats


def run_undo(
    path: str, session_path: str | None = None, dry: bool = False
) -> UndoStats:
    """Delete published posts from a tasks.json checkpoint.

    dry=True only reports what would be deleted (no login, no network).
    """
    if dry:
        tasks = load_tasks(path)
        _, stats = plan_undo(tasks)
        return stats
    return asyncio.run(_run_undo(path, session_path))
