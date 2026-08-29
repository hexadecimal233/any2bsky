"""Source-agnostic datasource abstraction.

A datasource knows how to turn ONE exported source directory (e.g. a QQ空间
backup) into the generic event stream (shared.event). The registry in
datasource/__init__.py registers available datasources at import time; the
terminal step runner (cli.py) runs pipeline steps against the selected one.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

from shared.event import Event, EventStream, SourceMeta
from shared.paths import events_path


class BaseDataSource(ABC):
    """Interface every datasource implements.

    Subclasses only implement `build_events` (+ optionally `account_title`);
    `convert` is generic: build events -> EventStream -> events.json (always
    written under ./data via shared.paths, never into the source dir).
    """

    source_type: str = ""  # registry key, e.g. "qzone"

    @abstractmethod
    def build_events(self, root: str) -> list[Event]:
        """Parse the export directory and return generic events."""

    def account_title(self, root: str) -> str:
        """Human label for the source account; "" if unknown."""
        return ""

    def convert(self, root: str, output_path: str | None = None) -> str:
        """Convert the export into one event-stream JSON file (data/<src>/events.json)."""
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            raise NotADirectoryError(f"not a directory: {root}")
        events = self.build_events(root)
        stream = EventStream(
            source=SourceMeta(
                type=self.source_type,
                root=root,
                title=self.account_title(root),
            ),
            events=events,
        )
        out_path = output_path or events_path(root)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        stream.write_json(out_path)
        return out_path
