"""Convert a QQ空间 backup export (qzoneexport/<qq>/) into the generic event stream.

The export layout we handle:
    <root>/
        Messages/json/messages.json  (说说, the user's own posts)
        Albums/json/albums.json     (相册, independent of 说说)
        Videos/json/videos.json     (视频)
        Shares/json/shares.json      (分享)
        Common/json/user.json       (account label)

Boards/json/boards.json (留言板) is DELIBERATELY NOT converted: it holds
visitor comments on the owner's board (external uin, often spam/ads), not
the owner's posts.

Each category becomes a flat list of events ordered by time, written to ONE
json file. Every event carries a `type` field for downstream filtering.

Album merge window:
- album_photo events whose timestamps fall within the same ALBUM_MERGE_WINDOW
  (10 minutes, anchored at the group's earliest event) collapse into ONE
  event; videos, boards and shares never merge.
- A merged group keeps the earliest `time` and concatenates all media/text in
  time order.

Minimal-principle rules:
- One directory in, one json out.
- Media are referenced by RELATIVE PATH (never remote URL).
- Images whose relative path does NOT exist on disk are OMITTED (the exporter
  may have failed to download them). This is validated at export time.
- No upload/length limits are applied; we just describe what exists.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from datasource.base import BaseDataSource
from shared.event import (
    Event,
    Media,
    RelativePath,
    RepostMeta,
)

# source-specific discriminator values carried in event.type
T_MOOD = "qzone.mood"  # 说说 (Messages/json/messages.json)
T_ALBUM = "qzone.album_photo"
T_VIDEO = "qzone.video"
T_SHARE = "qzone.share"

# album_photo events within this many seconds of a group's earliest event are
# merged into ONE event (10 minutes). Videos are NEVER merged.
ALBUM_MERGE_WINDOW = 600

# per-image alt text cap (app.bsky.embed.images#image alt maxLength)
ALT_TEXT_MAX = 1000


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _iso(ts: Any) -> datetime | None:
    """Parse a qzone timestamp into a timezone-aware datetime (UTC). None if absent."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        # "2025-06-01 04:35:15" -> "2025-06-01T04:35:15"
        if " " in s and "-" in s:
            s = s.replace(" ", "T", 1)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if "+" not in s and "T" in s:
            s = s + "+00:00"  # assume UTC for naive qzone strings
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _load(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _rel(root: str, path: str) -> str:
    """Return `path` relative to export root (forward slashes, no ./ prefix)."""
    if not path:
        return path
    p = path
    if os.path.isabs(p):
        try:
            p = os.path.relpath(p, root)
        except ValueError:
            pass
    return p.replace("\\", "/")


def _exists(root: str, rel_path: str) -> bool:
    if not rel_path:
        return False
    return os.path.exists(os.path.join(root, rel_path))


def _filter_media(root: str, paths: list[str]) -> list[Media]:
    """Keep only media paths that exist on disk; omit the rest. image kind."""
    out: list[Media] = []
    seen = set()
    for p in paths:
        r = _rel(root, p)
        if r and r not in seen and _exists(root, r):
            seen.add(r)
            kind = "video" if r.lower().endswith((".mp4", ".mov", ".webm")) else "image"
            out.append(Media(kind=kind, path=RelativePath(r)))
    return out


def _media_or_none(root: str, raw: str, kind: str) -> Media | None:
    """Return a typed Media if the file exists on disk, else None."""
    r = _rel(root, raw)
    if r and _exists(root, r):
        return Media(kind=kind, path=RelativePath(r))
    return None


# --------------------------------------------------------------------------- #
# Category extractors -> generic social-media Event
# --------------------------------------------------------------------------- #
def _extract_messages(root: str) -> list[Event]:
    """Extract the user's own 说说 (mood posts) from Messages/json/messages.json."""
    d = _load(os.path.join(root, "Messages", "json", "messages.json"))
    if not d:
        return []
    out: list[Event] = []
    for it in d if isinstance(d, list) else []:
        imgs = [
            im.get("custom_filepath") or im.get("custom_filename") or ""
            for im in it.get("custom_images", []) or []
        ]
        medias = _filter_media(root, imgs)
        for v in it.get("custom_videos", []) or []:
            fp = v.get("custom_filepath") or v.get("custom_filename") or ""
            pre = v.get("custom_pre_filepath") or v.get("custom_pre_filename") or ""
            vm = _media_or_none(root, fp, "video")
            pm = _media_or_none(root, pre, "image")
            if vm is not None and pm is not None:
                vm.poster = pm.path
            if vm is not None:
                medias.append(vm)
        out.append(
            Event(
                type=T_MOOD,
                time=_iso(it.get("custom_create_time") or it.get("createTime")),
                source="Messages",
                text=it.get("custom_content") or it.get("content") or "",
                medias=medias,
            )
        )
    return out


def _extract_albums(root: str) -> list[Event]:
    d = _load(os.path.join(root, "Albums", "json", "albums.json"))
    if not d:
        return []
    out: list[Event] = []
    for alb in d if isinstance(d, list) else []:
        for ph in alb.get("photoList", []):
            fp = ph.get("custom_filepath") or ph.get("custom_filename") or ""
            is_video = bool(ph.get("is_video"))
            media = _media_or_none(root, fp, "video" if is_video else "image")
            if media is not None:
                # photo captions go to the per-image ALT TEXT, never the post body
                media.alt = (ph.get("desc") or "")[:ALT_TEXT_MAX]
            out.append(
                Event(
                    type=T_ALBUM,
                    time=_iso(ph.get("uploadTime") or ph.get("shootTime")),
                    source="Albums",
                    text="",
                    medias=[media] if media is not None else [],
                )
            )
    return out


def _extract_videos(root: str) -> list[Event]:
    d = _load(os.path.join(root, "Videos", "json", "videos.json"))
    if not d:
        return []
    out: list[Event] = []
    for v in d if isinstance(d, list) else []:
        fp = v.get("custom_filepath") or v.get("custom_filename") or ""
        poster = v.get("custom_pre_filepath") or v.get("custom_pre_filename") or ""
        media = _media_or_none(root, fp, "video")
        poster_media = _media_or_none(root, poster, "image")
        if media is not None and poster_media is not None:
            media.poster = poster_media.path
        out.append(
            Event(
                type=T_VIDEO,
                time=_iso(v.get("uploadTime")),
                source="Videos",
                text=v.get("desc") or "",
                medias=[media] if media is not None else [],
            )
        )
    return out


def _extract_shares(root: str) -> list[Event]:
    """Shares are reposts of external content -> populate event-level `rt`."""
    d = _load(os.path.join(root, "Shares", "json", "shares.json"))
    if not d:
        return []
    out: list[Event] = []
    for s in d if isinstance(d, list) else []:
        src = s.get("source", {}) or {}
        imgs = [
            im.get("custom_filepath") or im.get("custom_filename") or ""
            for im in src.get("images", []) or []
        ]
        rt = RepostMeta(
            text=src.get("title") or "",
            url=src.get("url") or "",
            author=None,
            source=(src.get("from") or {}).get("name") or None,
        )
        out.append(
            Event(
                type=T_SHARE,
                time=_iso(s.get("shareTime")),
                source="Shares",
                text=s.get("desc") or "",
                medias=_filter_media(root, imgs),
                rt=rt,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Album merge window
# --------------------------------------------------------------------------- #
def _merge_group(group: list[Event]) -> Event:
    """Collapse a time-ordered group of album events into ONE event."""
    texts = [e.text for e in group if e.text]
    medias: list[Media] = []
    seen = set()
    for e in group:
        for m in e.medias:
            if m.path not in seen:
                seen.add(m.path)
                medias.append(m)

    return Event(
        type=T_ALBUM,
        time=group[0].time,  # group is time-ordered; earliest wins
        source=group[0].source,  # always "Albums"
        text="\n".join(texts),
        medias=medias,
        rt=next((e.rt for e in group if e.rt is not None), None),
    )


def _flush_group(group: list[Event]) -> list[Event]:
    """Turn a pending merge group into output events (1 group -> 1 event)."""
    if len(group) <= 1:
        return list(group)
    return [_merge_group(group)]


def _merge_albums(events: list[Event], window: int = ALBUM_MERGE_WINDOW) -> list[Event]:
    """Merge album_photo events that fall within one `window` (seconds).

    Anchor semantics: each group is anchored at its EARLIEST event; a later
    album event joins the group iff (its time - anchor) <= window, so a
    group's total span never exceeds `window`. Videos, boards and shares pass
    through untouched (never merged); album events without a timestamp are
    emitted as-is. Input MUST be time-ordered (build_events sorts it).
    """
    out: list[Event] = []
    group: list[Event] = []
    anchor: datetime | None = None

    for ev in events:
        if ev.type != T_ALBUM:
            out += _flush_group(group)
            group, anchor = [], None
            out.append(ev)
            continue
        t = ev.time
        if t is None:
            out += _flush_group(group)
            group, anchor = [], None
            out.append(ev)
            continue
        if anchor is not None and (t - anchor).total_seconds() > window:
            out += _flush_group(group)
            group, anchor = [], None
        if anchor is None:
            anchor = t
        group.append(ev)

    out += _flush_group(group)
    return out


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def _build_qzone_events(root: str, merge_window: int = ALBUM_MERGE_WINDOW) -> list:
    out: list[Event] = []
    out += _extract_messages(root)
    out += _extract_albums(root)
    out += _extract_videos(root)
    out += _extract_shares(root)

    def key(e: Event):
        return (1, "") if e.time is None else (0, e.time.isoformat())

    out.sort(key=key)
    return _merge_albums(out, window=merge_window)


def _account_title(root: str) -> str:
    u = _load(os.path.join(root, "Common", "json", "user.json"))
    if isinstance(u, dict):
        return u.get("nickname") or u.get("account") or u.get("qq") or ""
    return ""


class QzoneDataSource(BaseDataSource):
    """QQ空间 backup datasource (Boards=留言板 is deliberately ignored)."""

    source_type = "qzone"

    def build_events(
        self, root: str, merge_window: int = ALBUM_MERGE_WINDOW
    ) -> list[Event]:
        return _build_qzone_events(root, merge_window=merge_window)

    def account_title(self, root: str) -> str:
        return _account_title(root)
