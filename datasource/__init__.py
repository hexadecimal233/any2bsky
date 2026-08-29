"""Datasource registry -- datasources are registered here at import time.

Calling `get_datasource("qzone")` returns a fresh instance; `available()`
lists the registered keys. To add a datasource, drop a package under
datasource/ and import it at the bottom of this file (startup registration).
"""

from __future__ import annotations

from datasource.base import BaseDataSource

_REGISTRY: dict[str, type[BaseDataSource]] = {}


def register(cls: type[BaseDataSource]) -> type[BaseDataSource]:
    """Decorator/function: register a datasource by its `source_type`."""
    if not cls.source_type:
        raise ValueError(f"{cls.__name__} must define source_type")
    _REGISTRY[cls.source_type] = cls
    return cls


def get_datasource(source_type: str) -> BaseDataSource:
    try:
        return _REGISTRY[source_type]()
    except KeyError as e:
        raise KeyError(
            f"unknown datasource {source_type!r}; available: {available()}"
        ) from e


def available() -> list[str]:
    return sorted(_REGISTRY)


# --- startup registration of built-in datasources -------------------------- #
from datasource.qzone import QzoneDataSource

register(QzoneDataSource)
