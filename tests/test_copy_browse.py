"""Official filter HTML: parse real embedded catalog, never recommendation cards."""
import html
import urllib.parse
from types import SimpleNamespace

import pytest

from engine.manga import copy_web as cw


def test_copy_discovery_is_available_offline(monkeypatch):
    cw.clear_cache()
    monkeypatch.setattr(cw, "_get_path", lambda *args, **kwargs: pytest.fail("discovery performed network IO"))
    assert len(cw.web_categories()) == 70


def test_mobile_hides_web_source_but_preserves_legacy_data_adapter(monkeypatch):
    from engine import config
    from engine.manga import manager
    from engine.manga.copymanga import CopyManga
    from engine.manga.copymanga_web import CopyMangaWeb
    monkeypatch.setattr(config, "IS_MOBILE", True)
    monkeypatch.setattr(manager, "_REGISTRY", {"copymanga": CopyManga, "copymanga_web": CopyMangaWeb})
    assert [s["key"] for s in manager.list_adapters()] == ["copymanga"]
    assert manager.adapter_class("copymanga_web") is CopyMangaWeb


def test_site_categories_and_pagination(monkeypatch):
    cw.clear_cache()
    calls = []

    def get(path, **kwargs):
        calls.append(path)
        if path == "/comics":
            return SimpleNamespace(text='<a href="/comics?theme=aiqing&amp;ordering=-datetime_updated">愛情</a>')
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        assert query["theme"] == ["aiqing"]
        offset = query["offset"][0]
        rows = [{"path_word": "comic-" + offset, "name": "漫畫", "cover": "https://example.org/cover.jpg", "author": [{"name": "作者"}]}]
        return SimpleNamespace(text='<div class="exemptComicList"><a href="/comic/recommendation">推荐</a>'
            + '<div class="exemptComic-box" total="60" list="' + html.escape(repr(rows), quote=True) + '"></div></div>')

    monkeypatch.setattr(cw, "_get_path", get)
    cats = cw.web_categories(refresh=True)
    category = next(c["key"] for c in cats if c["name"] == "愛情")
    assert [c.id for c in cw.web_browse(category, 1)] == ["comic-0"]
    assert [c.id for c in cw.web_browse(category, 2)] == ["comic-30"]
    assert cw.web_browse(category, 1)[0].author == "作者"
    assert cw.web_categories() == cats
    assert calls.count("/comics") == 1
    cw.clear_cache()


def test_empty_catalog_is_valid_but_missing_or_executable_catalog_is_failure(monkeypatch):
    for body in ('<div class="exemptComic-box" total="0" list="[]"></div>',
                 '<div>推荐列表</div>',
                 '<div class="exemptComic-box" total="1" list="__import__(\'os\').getcwd()"></div>'):
        monkeypatch.setattr(cw, "_get_path", lambda *args, **kwargs: SimpleNamespace(text=body))
        if 'list="[]"' in body:
            assert cw.web_browse("ordering=-datetime_updated") == []
        else:
            with pytest.raises(cw.WebError):
                cw.web_browse("ordering=-datetime_updated")
