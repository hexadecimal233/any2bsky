"""Centralize where any2bsky writes its artifacts: ./data ONLY.

The source export directory (Downloads, etc.) is NEVER written to; every
artifact the pipeline produces lives under <workspace>/data/<source-basename>/:

    events.json      converter output
    tasks.json       planner output + executor checkpoint (resume input)
    tasks.dry.json   dry-run executor demo copy
    compressed/      AVIF compression outputs
"""

from __future__ import annotations

import os

_DATA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))


def data_root() -> str:
    return _DATA_ROOT


def data_dir_for(export_root: str) -> str:
    """Per-source artifact directory: <data_root>/<basename of export root>."""
    return os.path.join(_DATA_ROOT, os.path.basename(os.path.abspath(export_root)))


def ensure_data_dir(export_root: str) -> str:
    d = data_dir_for(export_root)
    os.makedirs(d, exist_ok=True)
    return d


def events_path(export_root: str) -> str:
    return os.path.join(data_dir_for(export_root), "events.json")


def tasks_path(export_root: str) -> str:
    return os.path.join(data_dir_for(export_root), "tasks.json")


def dry_tasks_path(export_root: str) -> str:
    return os.path.join(data_dir_for(export_root), "tasks.dry.json")


def compressed_dir(export_root: str) -> str:
    return os.path.join(data_dir_for(export_root), "compressed")
