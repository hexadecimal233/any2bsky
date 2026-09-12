import glob
import json
import os
from datetime import UTC, datetime
from typing import Any

from datasource.base import BaseDataSource
from shared.event import Event, Media, RelativePath, RepostMeta

_IMAGE_TYPES = {1, 3, 7}
_VIDEO_TYPES = {15}
_MUSIC_TYPES = {42, 47}

_VIDEO_EXTS = {".mp4", ".mov", ".webm"}


def _iso(ts: Any) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _find_export_json(root: str) -> str | None:
    for f in glob.glob(os.path.join(root, "*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            if isinstance(doc, dict) and "posts" in doc:
                return f
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _exists(root: str, rel_path: str) -> bool:
    return bool(rel_path) and os.path.exists(os.path.join(root, rel_path))


def _media_from_item(root: str, item: dict[str, str]) -> Media | None:
    local_path = item.get("localPath", "").replace("\\", "/")
    if not local_path or not _exists(root, local_path):
        return None
    ext = os.path.splitext(local_path)[1].lower()
    kind = "video" if ext in _VIDEO_EXTS else "image"
    return Media(kind=kind, path=RelativePath(local_path))


def _build_wechat_events(root: str) -> list[Event]:
    json_path = _find_export_json(root)
    if json_path is None:
        raise FileNotFoundError(
            f"no WeChat export JSON found in {root} "
            "(expected a file with a top-level 'posts' array)"
        )

    with open(json_path, "r", encoding="utf-8") as f:
        doc = json.load(f)

    posts: list[dict[str, Any]] = doc.get("posts", [])
    events: list[Event] = []

    for post in posts:
        post_type = post.get("type", 0)
        text = post.get("contentDesc", "")
        media_items = post.get("media", [])

        if post_type in _MUSIC_TYPES:
            url = media_items[0].get("url", "") if media_items else ""
            rt = RepostMeta(
                text=text or "",
                url=url,
                author=None,
                source="QQ音乐" if post_type == 42 else "网易云音乐",
            )
            events.append(
                Event(
                    time=_iso(post.get("createTime")),
                    source="朋友圈",
                    text=text,
                    medias=[],
                    rt=rt,
                )
            )
            continue

        if post_type in _IMAGE_TYPES or post_type in _VIDEO_TYPES:
            medias = [
                m
                for item in media_items
                if (m := _media_from_item(root, item)) is not None
            ]
            events.append(
                Event(
                    time=_iso(post.get("createTime")),
                    source="朋友圈",
                    text=text,
                    medias=medias,
                )
            )
            continue

        events.append(
            Event(
                time=_iso(post.get("createTime")),
                source="朋友圈",
                text=text,
                medias=[],
            )
        )

    events.sort(key=lambda e: (1, "") if e.time is None else (0, e.time.isoformat()))
    return events


def _account_title(root: str) -> str:
    json_path = _find_export_json(root)
    if json_path is None:
        return ""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        posts = doc.get("posts", [])
        if posts:
            return posts[0].get("nickname", "")
    except (OSError, json.JSONDecodeError):
        pass
    return ""


class WechatDataSource(BaseDataSource):
    source_type = "wechat"

    def build_events(self, root: str) -> list[Event]:
        return _build_wechat_events(root)

    def account_title(self, root: str) -> str:
        return _account_title(root)
