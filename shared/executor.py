"""DAG executor for post tasks (asyncio, checkpoint-resumable).

Scheduling (DAG):
- tasks form a DAG whose ONLY edge is `reply_to`; a task runs only after its
  parent has SUCCEEDED (parent.state == done / post_uri set).
- heavy tasks (media attached) run concurrently under `heavy_concurrency`;
  light tasks (no media) run serially, one at a time.

Posting:
- client=None -> DRY-RUN: builds the exact ``app.bsky.feed.post`` record,
  backfills synthetic post_uri/post_cid/parent_uri, marks the task done.
- authenticated ``AsyncClient`` (see shared.auth.login_client / --live):
  media are uploaded via com.atproto.repo.uploadBlob -- INCLUDING videos,
  matching the library's own ``send_video`` path (the server transcodes);
  the app.bsky.video multipart endpoints are NOT used (501 on bsky.social).
  The record is then created via com.atproto.repo.createRecord and the real
  uri/cid/parent_uri are backfilled.

Checkpointing:
- after EVERY task the whole array is written back to the checkpoint file
  ({"tasks": [...]}); resume skips every task whose state != pending.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from atproto import models as ap_models
from atproto_client.models.blob_ref import BlobRef, IpldLink

from shared.auth import SESSION_PATH, login_client
from shared.planner import (
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
    Task,
    load_tasks,
    write_tasks,
)


class ExecutorError(Exception):
    """Raised for DAG violations or record-assembly problems."""


# --------------------------------------------------------------------------- #
# Record assembly (exact app.bsky.feed.post payload; no network required)
# --------------------------------------------------------------------------- #
_MIME_BY_EXT = {
    ".avif": "image/avif",
    ".avifs": "image/avif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


def _mime(path: str) -> str:
    """MIME type for a media path (AVIF-aware; never guesses image/jpeg blindly)."""
    ext = os.path.splitext(path)[1].lower()
    t = _MIME_BY_EXT.get(ext)
    if t:
        return t
    t, _ = mimetypes.guess_type(path)
    return t or "application/octet-stream"


def _facet_for_link(text: str, url: str) -> ap_models.AppBskyRichtextFacet.Main | None:
    """Build a link facet for `url` inside `text` (UTF-8 byte offsets)."""
    idx = text.find(url)
    if idx < 0:
        return None
    start = len(text[:idx].encode("utf-8"))
    end = start + len(url.encode("utf-8"))
    return ap_models.AppBskyRichtextFacet.Main(
        index=ap_models.AppBskyRichtextFacet.ByteSlice(byte_start=start, byte_end=end),
        features=[ap_models.AppBskyRichtextFacet.Link(uri=url)],
    )


def build_record(
    task: Task,
    parent_uri: str | None = None,
    parent_cid: str | None = None,
    root_uri: str | None = None,
    root_cid: str | None = None,
    blob_ref: Callable[[str], BlobRef] | None = None,
) -> ap_models.AppBskyFeedPost.Record:
    """Assemble the lexicon record for one post using the atproto models.

    `blob_ref(path)` must return an uploaded blob (auth flow uploads via
    com.atproto.repo.uploadBlob / app.bsky.video and passes real BlobRefs).
    """
    record = ap_models.AppBskyFeedPost.Record(
        # restore the ORIGINAL publish time when the source event had one;
        # fall back to now only for events without a timestamp
        created_at=task.created_at or datetime.now(UTC).isoformat(),
        text=task.text,
    )

    facets = []
    if task.link_url:
        f = _facet_for_link(task.text, task.link_url)
        if f:
            facets.append(f)
    if facets:
        record.facets = facets

    if task.medias and blob_ref is not None:
        paths = task.medias
        alts = list(task.alts) + [""] * (len(paths) - len(task.alts))
        if len(paths) == 1 and paths[0].lower().endswith((".mp4", ".mov", ".webm")):
            record.embed = ap_models.AppBskyEmbedVideo.Main(
                video=blob_ref(paths[0]), alt=alts[0] or None
            )
        else:
            record.embed = ap_models.AppBskyEmbedImages.Main(
                images=[
                    ap_models.AppBskyEmbedImages.Image(
                        alt=alt, image=blob_ref(p), aspect_ratio=None
                    )
                    for p, alt in zip(paths, alts)
                ]
            )

    if parent_uri:
        record.reply = ap_models.AppBskyFeedPost.ReplyRef(
            parent=ap_models.ComAtprotoRepoStrongRef.Main(
                uri=parent_uri, cid=parent_cid or ""
            ),
            root=ap_models.ComAtprotoRepoStrongRef.Main(
                uri=root_uri or parent_uri, cid=root_cid or parent_cid or ""
            ),
        )
    return record


# --------------------------------------------------------------------------- #
# DAG executor
# --------------------------------------------------------------------------- #
@dataclass
class ExecutorStats:
    done: int = 0
    failed: int = 0
    skipped: int = 0
    heavy_peak_concurrency: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "done": self.done,
            "failed": self.failed,
            "skipped": self.skipped,
            "heavy_peak_concurrency": self.heavy_peak_concurrency,
        }


class DAGExecutor:
    """Async DAG executor over a flat task array with checkpoint resume."""

    def __init__(
        self,
        tasks: list[Task],
        checkpoint_path: str | None = None,
        heavy_concurrency: int = 2,
        client: Any | None = None,
        post_delay: float = 0.0,
        progress: bool = True,
    ) -> None:
        self.tasks = tasks
        self.checkpoint_path = checkpoint_path
        self.heavy_concurrency = max(1, heavy_concurrency)
        self.client = client
        self.post_delay = post_delay  # tests only: simulate network cost
        self.progress = progress
        self.by_id = {t.id: t for t in tasks}
        self.stats = ExecutorStats()
        self._heavy_running = 0
        self._last_report = 0.0

    # -- progress ------------------------------------------------------------
    def _report(self, force: bool = False) -> None:
        """Refresh a one-line progress indicator (~1/s) on stderr."""
        if not self.progress:
            return
        now = time.monotonic()
        if not force and now - self._last_report < 1.0:
            return
        self._last_report = now
        total = len(self.tasks)
        done = sum(1 for t in self.tasks if t.state == STATE_DONE)
        failed = sum(1 for t in self.tasks if t.state == STATE_FAILED)
        print(
            f"\r[live] {done}/{total} done, {failed} failed   ",
            file=sys.stderr,
            end="",
            flush=True,
        )

    # -- validation + topo order -------------------------------------------
    def _validate(self) -> None:
        ids = set(self.by_id)
        for t in self.tasks:
            if t.reply_to is not None and t.reply_to not in ids:
                raise ExecutorError(f"task {t.id} replies to unknown task {t.reply_to}")

    def topo_order(self) -> list[Task]:
        """Kahn topological order (stable: array order among ready nodes)."""
        self._validate()
        indeg = {t.id: 0 for t in self.tasks}
        children: dict[str, list[str]] = {t.id: [] for t in self.tasks}
        for t in self.tasks:
            if t.reply_to is not None:
                indeg[t.id] += 1
                children[t.reply_to].append(t.id)
        order: list[Task] = []
        ready = [t.id for t in self.tasks if indeg[t.id] == 0]
        while ready:
            nid = ready.pop(0)
            order.append(self.by_id[nid])
            for c in children[nid]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
        if len(order) != len(self.tasks):
            cyc = [t.id for t in self.tasks if t not in order]
            raise ExecutorError(f"cycle detected among tasks: {cyc[:5]}...")
        return order

    # -- helpers ------------------------------------------------------------
    def _resolve_root(self, t: Task) -> tuple[str | None, str | None]:
        """Walk reply_to to the chain root; returns (root_uri, root_cid)."""
        cur = t
        while cur.reply_to is not None:
            cur = self.by_id[cur.reply_to]
        return cur.post_uri, cur.post_cid

    def _dry_blob_ref(self, path: str) -> BlobRef:
        """Deterministic placeholder blob for dry-run mode."""
        return BlobRef(
            ref=IpldLink(link=f"dryrun:{path}"),
            mime_type=_mime(path),
            size=os.path.getsize(path),
        )

    async def _upload_media(self, path: str) -> BlobRef:
        """Upload one media file (image OR video) via com.atproto.repo.uploadBlob;
        returns the BlobRef. Videos use the same path as the library's send_video."""
        data = await asyncio.to_thread(self._read_file, path)
        resp = await self.client.upload_blob(data)
        return resp.blob

    @staticmethod
    def _read_file(path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    def checkpoint(self) -> None:
        if self.checkpoint_path:
            write_tasks(self.tasks, self.checkpoint_path)

    # -- execution -----------------------------------------------------------
    async def _execute(self, t: Task) -> None:
        try:
            parent_uri = parent_cid = None
            if t.reply_to is not None:
                parent = self.by_id[t.reply_to]
                if parent.state != STATE_DONE or not parent.post_uri:
                    raise ExecutorError(f"parent not done: {t.id} -> {t.reply_to}")
                parent_uri, parent_cid = parent.post_uri, parent.post_cid
            root_uri, root_cid = self._resolve_root(t)

            if self.post_delay:
                await asyncio.sleep(self.post_delay)

            if self.client is None:  # dry-run: fake success
                record = build_record(
                    t,
                    parent_uri,
                    parent_cid,
                    root_uri or parent_uri,
                    root_cid or parent_cid,
                    self._dry_blob_ref,
                )
                t.post_uri = f"at://did:plc:dryrun/app.bsky.feed.post/{t.id}"
                t.post_cid = "dry-run-cid"
            else:  # live posting
                uploads: dict[str, BlobRef] = {}
                for p in t.medias:
                    uploads[p] = await self._upload_media(p)
                record = build_record(
                    t,
                    parent_uri,
                    parent_cid,
                    root_uri or parent_uri,
                    root_cid or parent_cid,
                    lambda p: uploads[p],
                )
                resp = await self.client.com.atproto.repo.create_record(
                    ap_models.ComAtprotoRepoCreateRecord.Data(
                        repo=self.client.me.did,
                        collection="app.bsky.feed.post",
                        record=record,
                    )
                )
                t.post_uri = resp.uri
                t.post_cid = resp.cid
            t.parent_uri = parent_uri
            t.state = STATE_DONE
            self.stats.done += 1
        except Exception as e:  # noqa: BLE001
            t.state = STATE_FAILED
            t.fail_reason = f"{type(e).__name__}: {e}"
            self.stats.failed += 1

    async def run(self) -> ExecutorStats:
        """Execute all pending tasks in DAG (topological) order.

        Heavy tasks (with media) are submitted as concurrent workers bounded
        by `heavy_concurrency`; light tasks run serially inline; a child
        always waits for its parent's completion event.
        """
        order = self.topo_order()
        done_event = {t.id: asyncio.Event() for t in self.tasks}
        sem_heavy = asyncio.Semaphore(self.heavy_concurrency)
        heavy_workers: list[asyncio.Task] = []

        async def worker(t: Task) -> None:
            async with sem_heavy:
                self._heavy_running += 1
                self.stats.heavy_peak_concurrency = max(
                    self.stats.heavy_peak_concurrency, self._heavy_running
                )
                try:
                    await self._execute(t)
                finally:
                    self._heavy_running -= 1
            self.checkpoint()
            done_event[t.id].set()
            self._report()

        for t in order:
            if t.state != STATE_PENDING:
                if t.state == STATE_DONE:
                    self.stats.done += 1
                elif t.state == STATE_FAILED:
                    self.stats.failed += 1
                else:
                    self.stats.skipped += 1
                done_event[t.id].set()
                continue
            # a reply waits until its parent has actually been posted
            if t.reply_to is not None:
                await done_event[t.reply_to].wait()
                parent = self.by_id[t.reply_to]
                if parent.state != STATE_DONE:
                    t.state = STATE_FAILED
                    t.fail_reason = f"parent failed/skipped: {t.reply_to}"
                    self.stats.failed += 1
                    self.checkpoint()
                    done_event[t.id].set()
                    continue

            if t.medias:  # heavy: concurrent workers
                heavy_workers.append(asyncio.create_task(worker(t)))
            else:  # light: serial, inline
                await self._execute(t)
                self.checkpoint()
                done_event[t.id].set()
                self._report()

        if heavy_workers:
            await asyncio.gather(*heavy_workers)
        self._report(force=True)
        print(file=sys.stderr)  # newline after \r line
        return self.stats


def run_tasks_file(
    path: str,
    heavy_concurrency: int = 2,
    client: Any | None = None,
    dry_run: bool = True,
    post_delay: float = 0.0,
) -> ExecutorStats:
    """Load a tasks.json checkpoint, run pending tasks, write it back, resume-ready."""
    tasks = load_tasks(path)
    ex = DAGExecutor(
        tasks,
        checkpoint_path=path,
        heavy_concurrency=heavy_concurrency,
        client=client,
        post_delay=post_delay,
    )
    if not dry_run and client is None:
        raise ExecutorError("real posting needs an authenticated client (--live)")
    return asyncio.run(ex.run())


async def _run_live(
    path: str, heavy_concurrency: int, session_path: str | None
) -> ExecutorStats:
    # credentials are NOT accepted here: login must happen via the --login
    # step; this reuses the cached session (or raises AuthError with a hint).
    client = await login_client(session_path=session_path or SESSION_PATH)
    ex = DAGExecutor(
        load_tasks(path),
        checkpoint_path=path,
        heavy_concurrency=heavy_concurrency,
        client=client,
    )
    return await ex.run()


def run_live(
    path: str, heavy_concurrency: int = 2, session_path: str | None = None
) -> ExecutorStats:
    """Real posting: uses the cached session (see cli.py --login), uploads
    media, creates records, and checkpoints the task array."""
    return asyncio.run(_run_live(path, heavy_concurrency, session_path))
