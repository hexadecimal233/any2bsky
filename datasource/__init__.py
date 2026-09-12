from __future__ import annotations

from datasource.base import BaseDataSource

_REGISTRY: dict[str, type[BaseDataSource]] = {}


def register(cls: type[BaseDataSource]) -> type[BaseDataSource]:
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


from datasource.sources.qzone import QzoneDataSource
from datasource.sources.telegram import TelegramDataSource
from datasource.sources.wechat import WechatDataSource

register(QzoneDataSource)
register(TelegramDataSource)
register(WechatDataSource)
