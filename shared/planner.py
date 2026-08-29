"""Planner: turn generic Events into flat PDS-ready post tasks.

A post task is the smallest unit of work an uploader executes: ONE bluesky
post. Planning applies the PDS/app.bsky constraints:

  text    : <= 300 graphemes per post  (approximated by codepoints: exact for
            CJK, conservative for emoji ZWJ sequences)
  images  : <= 4 per post; each image encoded to AVIF, scaled to the long edge
            <= 4000px, file <= 2MB (quality stepped down from lossless by 10)
  video   : one video per post, NOT transcoded locally --- the official
            app.bsky.video pipeline transcodes it; duration >= 10 min or
            size > 300MB is a HARD failure (task.state = failed)

Rules fixed by user:
- Long text is split tweetstorm-style into a reply chain. The executor is a
  DAG scheduler: `reply_to` is the ONLY dependency edge (the previous post's
  task id); there are no thread_* fields. Once a post succeeds the executor
  backfills the record results on the task -- `post_uri` / `post_cid` and
  `parent_uri` (the parent post's record URI) -- so any reply can always
  resolve its parent from the task array.
- Repost/share content is DOWNGRADED (the quoted original has no bluesky
  counterpart): if rt has a URL, that URL is appended to the body and flagged
  via `link_url` for link-faceting; without a URL the rt title/source
  degrades into plain text. rt content is never quoted wholesale.
- Tasks are fully flat (only `medias` / `alts` are arrays). Every media path
  is ABSOLUTE. The task list is serialized as a linear JSON array
  {"tasks": [...]} and doubles as the checkpoint for resume: the executor
  skips every task whose state != "pending".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
import zlib
from dataclasses import dataclass, field, fields
from typing import Any

from atproto import models as ap_models  # lexicon metadata only (no network)

from shared.event import Event
from shared.paths import compressed_dir


# --------------------------------------------------------------------------- #
# PDS / app.bsky limits.
#
# Only MAX_IMAGES has a lexicon source inside the atproto library
# (app.bsky.embed.images.images maxLength=4), so it is read from the models
# at import time instead of being hand-copied. Everything else is a
# server/app-layer convention that the library does NOT encode; it is
# hardcoded once here with the source noted:
#   - text           300 graphemes (server-side; lexicon maxLength is 3000 codepoints)
#   - image size     2MB (raised from 1MB, official commit bluesky/atproto #4823)
#   - image edge     4000px (better-quality-photos update, 2026-04)
#   - video          300MB / 10min (official video limit update, 2026-08)
#   - alt text       1000 (no longer constrained by the lexicon; conservative cap)
# --------------------------------------------------------------------------- #
def _max_images_from_lexicon() -> int:
    """app.bsky.embed.images maxItems (4) read from the installed lexicon models."""
    info = ap_models.AppBskyEmbedImages.Main.model_fields["images"]
    for meta in info.metadata:
        n = getattr(meta, "max_length", None)
        if isinstance(n, int):
            return n
    return 4


MAX_IMAGES = _max_images_from_lexicon()
MAX_GRAPHEMES = 300  # app.bsky.feed.post server limit (graphemes)
MAX_IMAGE_PX = 4000  # long edge after resize (2026-04 limit)
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # per-image file limit (raised 1MB -> 2MB)
MAX_VIDEO_SECONDS = 10 * 60  # >= this => hard failure
MAX_VIDEO_BYTES = 300 * 1024 * 1024  # >  this => hard failure

AVIF_CRF_START = 0  # lossless
AVIF_CRF_STEP = 10
AVIF_CRF_MAX = 63

# task.state values
STATE_PENDING = "pending"
STATE_DONE = "done"
STATE_SKIPPED = "skipped"
STATE_FAILED = "failed"


class PlannerError(Exception):
    """Raised when a media cannot be made PDS-compliant at all."""


# --------------------------------------------------------------------------- #
# Task model (fully flat; only medias/alts are arrays)
# --------------------------------------------------------------------------- #
@dataclass
class Task:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: str = STATE_PENDING  # pending | done | skipped | failed
    type: str = "post"
    event_type: str = ""  # source event type, e.g. qzone.album_photo
    created_at: str | None = None  # original post time (ISO-8601), restored at publish
    text: str = ""
    medias: list[str] = field(default_factory=list)  # ABSOLUTE paths (media exception)
    alts: list[str] = field(default_factory=list)  # one alt per media, may be ""
    reply_to: str | None = None  # DAG edge: task id of the parent post
    link_url: str | None = None  # URL embedded in `text`; uploader link-facets it

    # backfilled by the executor after the post succeeds (lexicon record ids)
    post_uri: str | None = None  # at://.../app.bsky.feed.post/<rkey>
    post_cid: str | None = None
    parent_uri: str | None = (
        None  # resolved reply parent record URI (= reply_to's post_uri)
    )

    fail_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "type": self.type,
            "event_type": self.event_type,
            "created_at": self.created_at,
            "text": self.text,
            "medias": list(self.medias),
            "alts": list(self.alts),
            "reply_to": self.reply_to,
            "link_url": self.link_url,
            "post_uri": self.post_uri,
            "post_cid": self.post_cid,
            "parent_uri": self.parent_uri,
            "fail_reason": self.fail_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Task:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# --------------------------------------------------------------------------- #
# Text helpers (graphemes ~ codepoints; CJK-exact, emoji-conservative)
# --------------------------------------------------------------------------- #
def _glen(s: str) -> int:
    return len(s)


def _truncate(text: str, limit: int = MAX_GRAPHEMES) -> str:
    if _glen(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _split_paragraphs(text: str) -> list[str]:
    """Split into <=limit chunks, paragraph first, then sentence, then hard cut."""
    limit = MAX_GRAPHEMES
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n"):
        # keep sentence delimiters attached
        sents = re.split(r"(?<=[。！？!?；;])", para)
        sents = [s for s in sents if s]
        for sent in sents:
            if _glen(sent) > limit:  # long sentence: hard cut by codepoints
                rest = sent
                while _glen(rest) > limit:
                    chunks.append(rest[:limit])
                    rest = rest[limit:]
                sent = rest
            if not sent:
                continue
            if buf and _glen(buf) + 1 + _glen(sent) > limit:
                chunks.append(buf)
                buf = sent
            else:
                buf = (buf + "\n" + sent) if buf else sent
    if buf:
        chunks.append(buf)
    return chunks


def _tweetstorm(text: str) -> list[str]:
    """Split long text into numbered tweetstorm posts: ["1/3 ...", "2/3 ...", ...].

    Returns [text] unchanged when it already fits. Each chunk is re-trimmed so
    the "i/total " prefix keeps the post within MAX_GRAPHEMES.
    """
    if _glen(text) <= MAX_GRAPHEMES:
        return [text]
    raw = _split_paragraphs(text)
    total = len(raw)
    out = []
    for i, chunk in enumerate(raw, start=1):
        prefix = f"{i}/{total} "
        budget = MAX_GRAPHEMES - _glen(prefix)
        body = chunk if _glen(chunk) <= budget else chunk[:budget]
        out.append(prefix + body)
    return out


# --------------------------------------------------------------------------- #
# ffmpeg/ffprobe plumbing
# --------------------------------------------------------------------------- #
def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _image_size(path: str) -> tuple[int, int] | None:
    r = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            path,
        ]
    )
    if r.returncode != 0:
        return None
    parts = r.stdout.strip().split(",")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _video_duration(path: str) -> float | None:
    r = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            path,
        ]
    )
    if r.returncode != 0:
        return None
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def _scale_video(src: str, dst: str) -> bool:
    """(unused for now) placeholder for future local transcode if ever needed."""
    return False


# --------------------------------------------------------------------------- #
# Image AVIF compression (resize long edge -> AVIF, lossless->step-10)
# --------------------------------------------------------------------------- #
def _scale_long_edge(src: str, dst_png: str, max_px: int = MAX_IMAGE_PX) -> bool:
    r = _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            src,
            "-vf",
            f"scale={max_px}:{max_px}:force_original_aspect_ratio=decrease",
            "-frames:v",
            "1",
            dst_png,
        ]
    )
    return r.returncode == 0


def _to_avif(src: str, dst: str, crf: int) -> bool:
    r = _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            src,
            "-c:v",
            "libaom-av1",
            "-crf",
            str(crf),
            "-still-picture",
            "1",
            "-pix_fmt",
            "yuv420p",
            dst,
        ]
    )
    return r.returncode == 0


def _avif_out_name(src: str, out_dir: str, index: int = 0) -> str:
    stem = os.path.splitext(os.path.basename(src))[0]
    h = zlib.crc32(os.path.abspath(src).encode("utf-8")) & 0xFFFFFFFF
    suffix = f"_{index}" if index else ""
    return os.path.join(out_dir, f"{stem}_{h:08x}{suffix}.avif")


def _avif_cache_ok(dst: str) -> bool:
    """Cache = the deterministic output path already exists. Nothing else."""
    return os.path.exists(dst)


def compress_image(src: str, out_dir: str) -> str:
    """Make an image PDS-compliant. Returns ABSOLUTE path of the result.

    - pass 1: nothing done if already within limits (returns src).
    - pass 1: otherwise resize so the long edge <= 4000px (aspect preserved).
    - pass 2: encode AVIF starting from LOSSLESS (crf=0), stepping quality
      down by 10 until the file is <= 2MB.
    The output name derives from the source path (crc32), so re-planning the
    same export REUSES a previous AVIF instead of re-encoding it: the cache
    check is a plain path-exists probe.
    Raises PlannerError if the file still exceeds 2MB at the lowest quality.
    """
    size = _image_size(src)
    if size is None:
        raise PlannerError(f"cannot read dimensions: {src}")
    w, h = size
    bytes_size = os.path.getsize(src)
    if max(w, h) <= MAX_IMAGE_PX and bytes_size <= MAX_IMAGE_BYTES:
        return os.path.abspath(src)

    os.makedirs(out_dir, exist_ok=True)
    first_dst = _avif_out_name(src, out_dir, 0)
    if _avif_cache_ok(first_dst):
        return first_dst

    tmp_png = os.path.join(out_dir, f".tmp_{uuid.uuid4().hex}.png")
    work = src
    try:
        if max(w, h) > MAX_IMAGE_PX:
            if not _scale_long_edge(src, tmp_png):
                raise PlannerError(f"resize failed: {src}")
            work = tmp_png
        crf = AVIF_CRF_START
        attempt = 0
        while crf <= AVIF_CRF_MAX:
            dst = _avif_out_name(src, out_dir, attempt)
            if _avif_cache_ok(dst):
                return dst  # produced in a previous run
            if not _to_avif(work, dst, crf):
                raise PlannerError(f"avif encode failed (crf={crf}): {src}")
            if os.path.getsize(dst) <= MAX_IMAGE_BYTES:
                return dst
            crf += AVIF_CRF_STEP
            attempt += 1
        raise PlannerError(f"still >{MAX_IMAGE_BYTES} at crf={AVIF_CRF_MAX}: {src}")
    finally:
        if os.path.exists(tmp_png):
            os.remove(tmp_png)


def _prepare_media(
    ev: Event, root: str, compress_dir: str
) -> tuple[list[Task], list[Task]]:
    """Split one event's media into (image_tasks, video_tasks).

    Videos are emitted one-per-task WITHOUT transcoding (official pipeline
    transcodes); hard-failed ones become state=failed tasks. Image posts hold
    at most MAX_IMAGES images.
    """
    videos: list[Task] = []
    image_groups: list[list[tuple[str, str]]] = []  # [(abs_path, alt), ...]
    images: list[tuple[str, str]] = []
    ts = ev.time.isoformat() if ev.time is not None else None

    for m in ev.medias:
        p = os.path.abspath(os.path.join(root, str(m.path)))
        if m.kind == "video":
            t = Task(
                event_type=ev.type,
                created_at=ts,
                text="",
                medias=[p],
                alts=[m.alt or ""],
            )
            if os.path.getsize(p) > MAX_VIDEO_BYTES:
                t.state, t.fail_reason = (
                    STATE_FAILED,
                    (f"video >{MAX_VIDEO_BYTES // (1024 * 1024)}MB: {p}"),
                )
            else:
                dur = _video_duration(p)
                if dur is not None and dur >= MAX_VIDEO_SECONDS:
                    t.state, t.fail_reason = (
                        STATE_FAILED,
                        (f"video >= {MAX_VIDEO_SECONDS // 60}min: {p} ({dur:.1f}s)"),
                    )
                elif dur is None:
                    t.state, t.fail_reason = STATE_FAILED, f"cannot probe duration: {p}"
            videos.append(t)
        else:
            images.append((p, m.alt))

    for i in range(0, len(images), MAX_IMAGES):
        image_groups.append(images[i : i + MAX_IMAGES])

    image_tasks: list[Task] = []
    for group in image_groups:
        t = Task(
            event_type=ev.type,
            created_at=ts,
            text="",
            medias=[p for p, _ in group],
            alts=[a or "" for _, a in group],
        )
        compressed: list[str] = []
        alts: list[str] = []
        for p, alt in group:
            try:
                compressed.append(compress_image(p, compress_dir))
                alts.append(alt or "")
            except PlannerError as e:
                t.state, t.fail_reason = STATE_FAILED, str(e)
        t.medias, t.alts = compressed, alts
        image_tasks.append(t)
    return image_tasks, videos


# --------------------------------------------------------------------------- #
# One event -> flat task list (dependency graph flattened via order + reply_to)
# --------------------------------------------------------------------------- #
def _event_to_tasks(ev: Event, root: str, compress_dir: str) -> list[Task]:
    tasks: list[Task] = []

    # repost/share downgrade (the quoted original does NOT exist on bluesky):
    #  - rt has a URL   -> append the URL to the body and flag it for
    #    link-faceting via `link_url`; no other rt content enters the text.
    #  - rt has no URL  -> the rt title/source degrades into plain text.
    text = ev.text
    link_url: str | None = None
    if ev.rt is not None:
        rt = ev.rt
        url = (rt.url or "").strip()
        if url:
            link_url = url
            text = (text + "\n" + url) if text else url
        else:
            seg = []
            if (rt.text or "").strip():
                seg.append(rt.text.strip())
            if (rt.source or "").strip():
                seg.append(f"({rt.source.strip()})")
            extra = " ".join(seg)
            if extra:
                text = (text + "\n" + extra) if text else extra

    image_tasks, video_tasks = _prepare_media(ev, root, compress_dir)

    chunks = _tweetstorm(text)

    # Build the tweetstorm chain: reply_to is the only DAG edge
    prev_id: str | None = None
    body_tasks: list[Task] = []
    ts = ev.time.isoformat() if ev.time is not None else None
    for i, chunk in enumerate(chunks, start=1):
        t = Task(
            event_type=ev.type,
            created_at=ts,
            text=chunk,
            reply_to=prev_id,
        )
        body_tasks.append(t)
        prev_id = t.id

    # the link facet rides on whichever thread post actually contains the URL
    if link_url:
        for t in body_tasks:
            if link_url in t.text:
                t.link_url = link_url
                break

    # Attach the first media batch to the first body post (<=4 images fits)
    if body_tasks:
        first = body_tasks[0]
        if image_tasks:
            first_group = image_tasks[0]
            if first_group.state != STATE_FAILED:
                first.medias = first_group.medias
                first.alts = first_group.alts
                image_tasks = image_tasks[1:]
            # a failed image group stays its own state=failed task (not swallowed)

    tasks.extend(body_tasks)

    # Every extra pure-image post chains onto the trailing thread post, so all
    # posts derived from ONE event form a single reply DAG.
    parent_id = body_tasks[-1].id if body_tasks else None
    for g in image_tasks:
        if g.state == STATE_FAILED:
            tasks.append(g)  # failed posts stay standalone (never posted)
            continue
        if parent_id is not None:
            g.reply_to = parent_id
        tasks.append(g)
        parent_id = g.id

    tasks.extend(video_tasks)  # videos stay independent (one video per post)

    if not tasks:  # completely empty event -> single empty text post
        tasks.append(
            Task(
                event_type=ev.type,
                created_at=ev.time.isoformat() if ev.time is not None else None,
                text="",
            )
        )
    return tasks


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def plan_events(
    events: list[Event], export_root: str, compress_dir: str | None = None
) -> list[Task]:
    """Convert events into a flat, PDS-compliant task array.

    Every media path in the result is ABSOLUTE. Failed media checks (video
    too long/too big, image uncompressible) are kept as state=failed tasks.
    Compression outputs default to data/<source>/compressed (shared.paths),
    never into the source export directory.
    """
    root = os.path.abspath(export_root)
    out_dir = os.path.abspath(compress_dir) if compress_dir else compressed_dir(root)
    return [t for ev in events for t in _event_to_tasks(ev, root, out_dir)]


def write_tasks(tasks: list[Task], path: str) -> str:
    """Serialize tasks as a LINEAR array for checkpoint/resume: {"tasks":[...]}."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"tasks": [t.to_dict() for t in tasks]}, f, ensure_ascii=False, indent=2
        )
    return path


def load_tasks(path: str) -> list[Task]:
    """Restore tasks from a checkpoint file (resume from first non-terminal state)."""
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return [Task.from_dict(t) for t in d.get("tasks", [])]
