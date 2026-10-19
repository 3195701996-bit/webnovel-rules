import hashlib
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def metadata_env(tmp_path, monkeypatch):
    import app
    import server.novel_api as api
    folder = tmp_path / "book-one"
    folder.mkdir()
    info = {"name": "https://metadata.invalid/1", "author": "", "book_url": "https://metadata.invalid/1", "source_uid": "source"}
    (folder / "_state.json").write_text(json.dumps({"book": info, "chapters": [{"url": "chapter"}], "completed": ["chapter"]}))
    (folder / "chapter.cache").write_text("保留原文")
    (folder / "_progress.json").write_text('{"idx":13}')
    monkeypatch.setattr(api, "BOOKS_DIR", str(tmp_path))
    monkeypatch.setattr(api, "get_by_uid", lambda uid: {"uid": uid})
    return app.app.test_client(), api, folder


def test_metadata_preview_is_bounded_and_does_not_touch_local_files(metadata_env, monkeypatch):
    client, api, folder = metadata_env
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    calls = []
    class Crawler:
        def __init__(self, source): pass
        def get_book(self, url, **options):
            calls.append((url, options))
            return SimpleNamespace(name=" 真正\n书名 ", author="作者", chapters=["must-not-import"])
    monkeypatch.setattr(api, "SourceCrawler", Crawler)
    response = client.get("/api/books/book-one/metadata/preview")
    assert response.status_code == 200
    result = response.get_json()
    assert result["read_only"] is True
    assert result["proposed"] == {"name": "真正 书名", "author": "作者"}
    assert {item["field"] for item in result["changes"]} == {"name", "author"}
    assert result["state_revision"] == hashlib.sha256(before["_state.json"]).hexdigest()
    assert calls == [("https://metadata.invalid/1", {"fast": True, "deadline": 12})]
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


@pytest.mark.parametrize("name", ["", "https://metadata.invalid/1", "https://other.invalid", None])
def test_unusable_source_title_cannot_be_presented_as_a_repair(metadata_env, monkeypatch, name):
    client, api, folder = metadata_env
    before = (folder / "_state.json").read_bytes()
    monkeypatch.setattr(api, "SourceCrawler", lambda source: SimpleNamespace(get_book=lambda *a, **k: SimpleNamespace(name=name, author="作者")))
    assert client.get("/api/books/book-one/metadata/preview").status_code == 502
    assert (folder / "_state.json").read_bytes() == before


def test_corrupt_state_is_preserved_without_source_access(metadata_env, monkeypatch):
    client, api, folder = metadata_env
    (folder / "_state.json").write_bytes(b'{broken')
    monkeypatch.setattr(api, "get_by_uid", lambda uid: pytest.fail("bad state must not access source"))
    assert client.get("/api/books/book-one/metadata/preview").status_code == 503
    assert (folder / "_state.json").read_bytes() == b'{broken'


def test_missing_source_and_network_failure_preserve_all_files(metadata_env, monkeypatch):
    client, api, folder = metadata_env
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    monkeypatch.setattr(api, "get_by_uid", lambda uid: None)
    monkeypatch.setattr(api, "find_source", lambda url: None)
    assert client.get("/api/books/book-one/metadata/preview").status_code == 409
    monkeypatch.setattr(api, "get_by_uid", lambda uid: {"uid": uid})
    def fail(*args, **kwargs):
        raise TimeoutError("fixture timeout")
    monkeypatch.setattr(api, "SourceCrawler", lambda source: SimpleNamespace(get_book=fail))
    assert client.get("/api/books/book-one/metadata/preview").status_code == 502
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
