import os
from abc import ABC, abstractmethod

from shared.event import Event, EventStream, SourceMeta
from shared.paths import events_path


class BaseDataSource(ABC):
    source_type: str = ""

    @abstractmethod
    def build_events(self, root: str) -> list[Event]: ...

    def account_title(self, root: str) -> str:
        return ""

    def convert(self, root: str, output_path: str | None = None) -> str:
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
