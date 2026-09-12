import glob
import json
import os
from datetime import datetime
from typing import Any

from datasource.base import BaseDataSource
from shared.event import Event, Media, RelativePath

_VIDEO_EXTS = {".mp4", ".mov", ".webm"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}


def _iso(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        s = str(ts).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _find_messages_json(root: str) -> str | None:
    direct = os.path.join(root, "messages.json")
    if os.path.isfile(direct):
        return direct
    for f in glob.glob(os.path.join(root, "*.json")):
        if os.path.basename(f) == "messages.json":
            return f
        try:
            with open(f, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            if isinstance(doc, list):
                return f
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _attachment_rel(root: str, attachment_path: str) -> str | None:
    p = attachment_path.replace("\\", "/")
    if os.path.isabs(p):
        try:
            p = os.path.relpath(p, root)
        except ValueError:
            return None
    elif not p.startswith("attachments/"):
        p = os.path.join("attachments", p)
    return p.replace("\\", "/")


def _media_from_attachment(root: str, attachment_path: str) -> Media | None:
    rel = _attachment_rel(root, attachment_path)
    if not rel or not os.path.exists(os.path.join(root, rel)):
        return None
    ext = os.path.splitext(rel)[1].lower()
    if ext in _VIDEO_EXTS:
        kind = "video"
    elif ext in _IMAGE_EXTS:
        kind = "image"
    else:
        return None
    return Media(kind=kind, path=RelativePath(rel))


def _text_of(msg: dict[str, Any]) -> str:
    return (msg.get("message") or msg.get("text") or "").strip()


def _build_telegram_events(root: str) -> list[Event]:
    json_path = _find_messages_json(root)
    if json_path is None:
        raise FileNotFoundError(f"no messages.json found in {root}")

    with open(json_path, "r", encoding="utf-8") as f:
        doc = json.load(f)

    messages = doc if isinstance(doc, list) else []
    events: list[Event] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("action"):  # service messages (join/pin/...)
            continue
        if msg.get("comment_of") is not None:  # visitor comments on channel posts
            continue
        text = _text_of(msg)
        attachment_path = msg.get("attachment_path")
        medias = []
        if attachment_path:
            m = _media_from_attachment(root, str(attachment_path))
            if m is not None:
                medias = [m]
        if not text and not medias:
            continue
        events.append(
            Event(
                time=_iso(msg.get("date")),
                source="messages",
                text=text,
                medias=medias,
            )
        )

    events.sort(key=lambda e: (1, "") if e.time is None else (0, e.time.isoformat()))
    return events


class TelegramDataSource(BaseDataSource):
    source_type = "telegram"

    def build_events(self, root: str) -> list[Event]:
        return _build_telegram_events(root)

    def account_title(self, root: str) -> str:
        return os.path.basename(os.path.abspath(root))
