import json
import os
from datetime import UTC, datetime
from typing import Any

from datasource.base import BaseDataSource
from shared.event import Event, Media, RelativePath, RepostMeta

ALBUM_MERGE_WINDOW = 600
ALT_TEXT_MAX = 1000


def _iso(ts: Any) -> datetime | None:
    if ts is None or ts == "":
        return None
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        if " " in s and "-" in s:
            s = s.replace(" ", "T", 1)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if "+" not in s and "T" in s:
            s += "+00:00"
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
    return bool(rel_path) and os.path.exists(os.path.join(root, rel_path))


def _filter_media(root: str, paths: list[str]) -> list[Media]:
    out: list[Media] = []
    seen: set[str] = set()
    for p in paths:
        r = _rel(root, p)
        if r and r not in seen and _exists(root, r):
            seen.add(r)
            kind = "video" if r.lower().endswith((".mp4", ".mov", ".webm")) else "image"
            out.append(Media(kind=kind, path=RelativePath(r)))
    return out


def _media_or_none(root: str, raw: str, kind: str) -> Media | None:
    r = _rel(root, raw)
    if r and _exists(root, r):
        return Media(kind=kind, path=RelativePath(r))
    return None


def _extract_messages(root: str) -> list[Event]:
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
                media.alt = (ph.get("desc") or "")[:ALT_TEXT_MAX]
            out.append(
                Event(
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
                time=_iso(v.get("uploadTime")),
                source="Videos",
                text=v.get("desc") or "",
                medias=[media] if media is not None else [],
            )
        )
    return out


def _extract_shares(root: str) -> list[Event]:
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
                time=_iso(s.get("shareTime")),
                source="Shares",
                text=s.get("desc") or "",
                medias=_filter_media(root, imgs),
                rt=rt,
            )
        )
    return out


def _merge_group(group: list[Event]) -> Event:
    texts = [e.text for e in group if e.text]
    medias: list[Media] = []
    seen: set[str] = set()
    for e in group:
        for m in e.medias:
            if m.path not in seen:
                seen.add(m.path)
                medias.append(m)
    return Event(
        time=group[0].time,
        source=group[0].source,
        text="\n".join(texts),
        medias=medias,
        rt=next((e.rt for e in group if e.rt is not None), None),
    )


def _flush_group(group: list[Event]) -> list[Event]:
    return [_merge_group(group)] if len(group) > 1 else list(group)


def _merge_albums(events: list[Event], window: int = ALBUM_MERGE_WINDOW) -> list[Event]:
    # Groups are anchored at their earliest album event; a later album event
    # joins while its offset from the anchor is <= window. Non-album events
    # pass through untouched.
    out: list[Event] = []
    group: list[Event] = []
    anchor: datetime | None = None

    for ev in events:
        if ev.source != "Albums":
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


def _build_qzone_events(
    root: str, merge_window: int = ALBUM_MERGE_WINDOW
) -> list[Event]:
    out: list[Event] = []
    out += _extract_messages(root)
    out += _extract_albums(root)
    out += _extract_videos(root)
    out += _extract_shares(root)
    out.sort(key=lambda e: (1, "") if e.time is None else (0, e.time.isoformat()))
    return _merge_albums(out, window=merge_window)


def _account_title(root: str) -> str:
    u = _load(os.path.join(root, "Common", "json", "user.json"))
    if isinstance(u, dict):
        return u.get("nickname") or u.get("account") or u.get("qq") or ""
    return ""


class QzoneDataSource(BaseDataSource):
    source_type = "qzone"

    def build_events(
        self, root: str, merge_window: int = ALBUM_MERGE_WINDOW
    ) -> list[Event]:
        return _build_qzone_events(root, merge_window=merge_window)

    def account_title(self, root: str) -> str:
        return _account_title(root)
