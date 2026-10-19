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
    from engine.novel_metadata import metadata_snapshot
    assert result["state_revision"] == metadata_snapshot(str(folder / "_state.json"))[1]
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


def test_confirmed_metadata_is_separate_and_survives_crawler_state_save(metadata_env):
    client, api, folder = metadata_env
    from engine.novel_metadata import metadata_snapshot
    from engine.crawler import Book, CrawlTask
    from server.state import load_book_state
    path = str(folder / '_state.json')
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    revision = metadata_snapshot(path)[1]
    response = client.post('/api/books/book-one/metadata', json={
        'state_revision': revision, 'metadata': {'name': '确认书名', 'author': '确认作者'}})
    assert response.status_code == 200
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.name != '_metadata.json'} == before
    assert load_book_state(path)['book']['name'] == '确认书名'
    stale = client.post('/api/books/book-one/metadata', json={
        'state_revision': revision, 'metadata': {'name': '过期写入'}})
    assert stale.status_code == 409
    source = {'uid': 'source', 'bookSourceUrl': 'https://metadata.invalid'}
    task = CrawlTask(source, 'https://metadata.invalid/1', str(folder))
    task.book = Book(source, task.book_url)
    task.book.name = '下载器旧书名'
    task.book.chapters = [{'name': '新章', 'url': 'chapter'}]
    task.completed = ['chapter']
    task._save_state()
    assert task.book.name == '确认书名' and task.book.author == '确认作者'
    assert load_book_state(path)['book']['name'] == '确认书名'
    assert (folder / 'chapter.cache').read_bytes() == before['chapter.cache']
    assert (folder / '_progress.json').read_bytes() == before['_progress.json']


@pytest.mark.parametrize('fields', [{'chapters': ['bad']}, {'name': ''}, {'name': ' https://wrong.invalid '}, {'name': 1}, {}])
def test_confirmation_rejects_non_metadata_changes(metadata_env, fields):
    client, api, folder = metadata_env
    from engine.novel_metadata import metadata_snapshot
    revision = metadata_snapshot(str(folder / '_state.json'))[1]
    response = client.post('/api/books/book-one/metadata', json={'state_revision': revision, 'metadata': fields})
    assert response.status_code == 400
    assert not (folder / '_metadata.json').exists()


def test_confirmation_refuses_changed_catalog_and_corrupt_metadata_record(metadata_env):
    client, api, folder = metadata_env
    from engine.novel_metadata import metadata_snapshot
    revision = metadata_snapshot(str(folder / '_state.json'))[1]
    (folder / '_state.json').write_text((folder / '_state.json').read_text() + ' ')
    response = client.post('/api/books/book-one/metadata', json={'state_revision': revision, 'metadata': {'name': '书名'}})
    assert response.status_code == 409
    assert not (folder / '_metadata.json').exists()
    original = b'{bad metadata'
    (folder / '_metadata.json').write_bytes(original)
    response = client.post('/api/books/book-one/metadata', json={'state_revision': revision, 'metadata': {'name': '书名'}})
    assert response.status_code == 400
    assert (folder / '_metadata.json').read_bytes() == original


@pytest.mark.parametrize('original', [b'', b' ', b'null', b'[]',
    b'{"version":true,"book_url":"https://metadata.invalid/1","metadata":{"name":"bad"}}',
    b'{"version":1,"book_url":"https://metadata.invalid/1","metadata":{}}',
    b'{"version":1,"book_url":"https://metadata.invalid/1","metadata":{"name":"bad","chapters":[]}}',
    b'{"version":1,"book_url":"https://metadata.invalid/1","metadata":{"name":7}}'])
def test_existing_invalid_metadata_is_never_overwritten(metadata_env, original):
    client, api, folder = metadata_env
    from engine.novel_metadata import metadata_snapshot, overlay_metadata
    path = folder / '_state.json'
    revision = metadata_snapshot(str(path))[1]
    (folder / '_metadata.json').write_bytes(original)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    raw = json.loads(path.read_text())
    assert overlay_metadata(str(path), raw) == raw
    assert client.get('/api/books/book-one/metadata/preview').status_code == 503
    response = client.post('/api/books/book-one/metadata', json={
        'state_revision': revision, 'metadata': {'name': '不能覆盖异常记录'}})
    assert response.status_code == 400
    assert before == {p.name: p.read_bytes() for p in folder.iterdir()}
