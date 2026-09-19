# -*- coding: utf-8 -*-
"""漫画引擎：适配器型漫画源（仿 Cimoc MangaParser / venera ComicSource 接口）"""
from .base import (  # noqa: F401
    Comic, ComicDetails, Chapter, MangaAdapter, MangaError,
)
from .manager import get_adapter, list_adapters, register_adapter  # noqa: F401
