from engine.app_utils import chapter_cache_valid, chapter_cache_keys


def test_validation_tracks_repair_and_corruption(tmp_path):
    path = tmp_path / "chapter.cache"
    path.write_text("正文内容", encoding="utf-8")
    assert chapter_cache_valid(path)
    path.write_bytes(b"\xff" * 12)
    assert not chapter_cache_valid(path)
    assert chapter_cache_keys(tmp_path) == set()
    replacement = tmp_path / "replacement"
    replacement.write_text("修复正文", encoding="utf-8")
    replacement.replace(path)
    assert chapter_cache_valid(path)
    assert chapter_cache_keys(tmp_path) == {"chapter"}


def test_validation_checks_end_and_unicode_chunk_boundary(tmp_path):
    path = tmp_path / "long.cache"
    path.write_text("中" * 70000, encoding="utf-8")
    assert chapter_cache_valid(path)
    with path.open("ab") as stream:
        stream.write(b"\xff")
    assert not chapter_cache_valid(path)


def test_whitespace_and_missing_are_unavailable(tmp_path):
    path = tmp_path / "chapter.cache"
    assert not chapter_cache_valid(path)
    path.write_text(" \n\t\u3000" * 20000, encoding="utf-8")
    assert not chapter_cache_valid(path)
    path.unlink()
    path.mkdir()
    assert not chapter_cache_valid(path)
