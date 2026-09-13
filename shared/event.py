"""Source-agnostic social-media event-stream schema.

An event stream is a JSON document:

    {
      "version": "v1",
      "source": { "type": "qzone", "root": "/abs/path", "title": "..." },
      "generated_at": "2025-...Z",
      "count": 250,
      "events": [ Event, ... ]
    }

An Event is:
    {
      "time": str | null,          # ISO-8601 UTC datetime
      "source": str,               # which sub-folder/file it came from
      "text": str,                 # post body, "" if none
      "medias": [ Media, ... ],
      "rt": RepostMeta | null,     # repost/quote metadata
    }

Media: { "kind": "image" | "video", "path": RelativePath, "alt": str, "poster": RelativePath | null }
RepostMeta: { "text": str, "url": str, "author": str | null, "source": str | null }
"""

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Self

VERSION = "v1"


# --------------------------------------------------------------------------- #
# Constrained primitives
# --------------------------------------------------------------------------- #
class RelativePath(str):
    """A path relative to the source export root."""

    def __new__(cls, value: str) -> Self:
        s = value.replace("\\", "/")
        if s == "":
            raise ValueError("RelativePath must not be empty")
        if s.startswith(("/", "\\")):
            raise ValueError(f"RelativePath must not be absolute: {value!r}")
        if "://" in s or s.startswith("//"):
            raise ValueError(f"RelativePath must not be a URL: {value!r}")
        return super().__new__(cls, s)


FilePath: type = RelativePath


# --------------------------------------------------------------------------- #
# Source meta
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SourceMeta:
    type: str  # converter id, e.g. "qzone"
    root: str  # ABSOLUTE path to the export root
    title: str = ""

    def __post_init__(self) -> None:
        if not os.path.isabs(self.root):
            raise ValueError(f"SourceMeta.root must be absolute: {self.root!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "root": self.root, "title": self.title}


# --------------------------------------------------------------------------- #
# Media (discriminated by kind)
# --------------------------------------------------------------------------- #
@dataclass
class Media:
    kind: str  # "image" | "video"
    path: FilePath
    alt: str = ""
    poster: FilePath | None = None  # for video thumbnails

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": str(self.path),
            "alt": self.alt,
            "poster": str(self.poster) if self.poster is not None else None,
        }


# --------------------------------------------------------------------------- #
# Repost / quote metadata (event-level)
# --------------------------------------------------------------------------- #
@dataclass
class RepostMeta:
    text: str = ""
    url: str = ""
    author: str | None = None
    source: str | None = None  # original platform/app, e.g. "网易云音乐"

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "url": self.url,
            "author": self.author,
            "source": self.source,
        }


# --------------------------------------------------------------------------- #
# Generic social-media Event
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    time: datetime | None
    source: str
    text: str = ""
    medias: list[Media] = field(default_factory=list)
    rt: RepostMeta | None = None  # event-level repost metadata

    def to_dict(self) -> dict[str, Any]:
        return {
            "time": self.time.isoformat() if self.time is not None else None,
            "source": self.source,
            "text": self.text,
            "medias": [m.to_dict() for m in self.medias],
            "rt": self.rt.to_dict() if self.rt is not None else None,
        }


def is_uncommented_repost(ev: Event) -> bool:
    """True for a repost/share the user added no text of their own to."""
    return ev.rt is not None and not ev.text.strip()


# --------------------------------------------------------------------------- #
# Stream
# --------------------------------------------------------------------------- #
@dataclass
class EventStream:
    source: SourceMeta
    events: list[Event] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "source": self.source.to_dict(),
            "generated_at": datetime.now(UTC).isoformat(),
            "count": len(self.events),
            "events": [e.to_dict() for e in self.events],
        }

    def write_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)


def load_events(path: str, skip_dropped: bool = True) -> tuple[list[Event], int]:
    """Load a converted events.json back into typed Events.

    `skip_dropped=True` drops events whose manual-filter flag
    {"drop": true} is set (see tools/editor.html). Returns (kept, dropped).
    """
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)

    kept: list[Event] = []
    dropped = 0
    for it in doc.get("events", []):
        if skip_dropped and it.get("drop"):
            dropped += 1
            continue
        kept.append(_event_from_dict(it))
    return kept, dropped


def _event_from_dict(it: dict[str, Any]) -> Event:
    medias = [
        Media(
            kind=m["kind"],
            path=RelativePath(m["path"]),
            alt=m.get("alt", ""),
            poster=RelativePath(m["poster"]) if m.get("poster") else None,
        )
        for m in it.get("medias", [])
    ]
    rt = None
    r = it.get("rt")
    if r:
        rt = RepostMeta(
            text=r.get("text", ""),
            url=r.get("url", ""),
            author=r.get("author"),
            source=r.get("source"),
        )
    t = None
    ts = it.get("time")
    if ts:
        try:
            t = datetime.fromisoformat(
                ts if ts.endswith("+00:00") else ts.replace("Z", "+00:00")
            )
        except ValueError:
            t = None
    return Event(
        time=t,
        source=it.get("source", ""),
        text=it.get("text", ""),
        medias=medias,
        rt=rt,
    )
