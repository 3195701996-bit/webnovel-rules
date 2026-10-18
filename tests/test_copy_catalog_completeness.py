import pytest

from engine.manga.copymanga import CopyManga
from engine.manga.base import MangaError


@pytest.mark.parametrize("method", ["_app_fetch_chapters", "_app_comic_info"])
@pytest.mark.parametrize("failure", ["network", "empty", "success"])
def test_app_catalog_never_returns_partial_success(monkeypatch, method, failure):
    adapter = object.__new__(CopyManga)
    monkeypatch.setattr(CopyManga, "api_url", property(lambda self: "https://catalog.test"))
    monkeypatch.setattr(adapter, "_get_request_id", lambda: "request")

    def get(url):
        if "/comic2/" in url:
            return {"results": {"comic": {}, "groups": {
                "default": {"path_word": "default", "name": "默认"}}}}
        if "offset=0&" in url:
            return {"results": {"total": 101, "list": [
                {"uuid": str(i), "name": f"第{i}话"} for i in range(100)]}}
        if failure == "network":
            raise OSError("offline")
        if failure == "success":
            return {"results": {"total": 101, "list": [
                {"uuid": "100", "name": "第100话"}]}}
        return {"results": {"total": 101, "list": []}}

    monkeypatch.setattr(adapter, "_get", get)
    if failure == "success":
        result = getattr(adapter, method)("comic")
        chapters = result if isinstance(result, list) else result.chapters
        assert len(chapters) == 101
        assert {chapter.id for chapter in chapters} == {str(i) for i in range(101)}
    else:
        with pytest.raises(MangaError, match="未取全"):
            getattr(adapter, method)("comic")
