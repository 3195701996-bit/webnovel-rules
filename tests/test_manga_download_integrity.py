"""下载页数元数据的持久化契约。"""
import json


def test_page_count_is_written_atomically_to_matching_chapter_only(tmp_path):
    from engine.manga.download_manager import _record_download_page_count

    info_path = tmp_path / "_info.json"
    info_path.write_text(json.dumps({"title": "fixture", "chapters": [
        {"id": "chapter-a", "name": "第1话"},
        {"id": "chapter-b", "name": "第2话", "download_page_count": 4},
    ]}), encoding="utf-8")

    assert _record_download_page_count(info_path, "chapter-a", 7)
    saved = json.loads(info_path.read_text(encoding="utf-8"))
    assert saved["title"] == "fixture"
    assert saved["chapters"][0]["download_page_count"] == 7
    assert saved["chapters"][1]["download_page_count"] == 4
    assert not list(tmp_path.glob("*.tmp"))


def test_page_count_write_rejects_missing_chapter_and_malformed_count(tmp_path):
    from engine.manga.download_manager import _record_download_page_count

    info_path = tmp_path / "_info.json"
    original = json.dumps({"chapters": [{"id": "chapter-a"}]})
    info_path.write_text(original, encoding="utf-8")
    assert not _record_download_page_count(info_path, "missing", 3)
    assert not _record_download_page_count(info_path, "chapter-a", "bad")
    assert info_path.read_text(encoding="utf-8") == original


def test_downloader_image_magic_accepts_gif_variants_and_rejects_bad_headers(tmp_path):
    from engine.manga.downloader import _valid_image_header

    for name, signature in (("gif87.gif", b"GIF87a"),
                            ("gif89.gif", b"GIF89a")):
        path = tmp_path / name
        path.write_bytes(signature + b"image-data")
        assert _valid_image_header(path), f"valid {name} was rejected"

    bad = tmp_path / "html.gif"
    bad.write_bytes(b"GIF" + b"x" * 16)
    assert not _valid_image_header(bad)


def test_downloaded_page_indices_count_unique_valid_pages_across_formats(tmp_path):
    from engine.manga.downloader import _downloaded_page_indices

    signatures = {
        ".jpg": b"\xff\xd8\xff" + b"x" * 16,
        ".png": b"\x89PNG\r\n\x1a\n" + b"x" * 16,
        ".gif": b"GIF89a" + b"x" * 16,
        ".webp": b"RIFF" + b"x\x00\x00\x00WEBP" + b"x" * 8,
        ".avif": b"\x00\x00\x00\x18ftypavif" + b"x" * 8,
    }
    for index, extension in enumerate(signatures):
        (tmp_path / f"{index:04d}{extension}").write_bytes(signatures[extension])
    # A second valid representation is still the same page, not an extra page.
    (tmp_path / "0000.webp").write_bytes(signatures[".webp"])
    # A numbered corrupt file and an unrelated filename do not satisfy a page.
    (tmp_path / "0005.jpg").write_bytes(b"not-an-image" * 2)
    (tmp_path / "0006.txt").write_bytes(signatures[".jpg"])
    (tmp_path / "cover.jpg").write_bytes(signatures[".jpg"])

    assert _downloaded_page_indices(str(tmp_path)) == set(range(5))


def test_partial_recovery_page_indices_expose_gaps_and_not_duplicate_files(tmp_path):
    from engine.manga.downloader import _downloaded_page_indices

    (tmp_path / "0000.avif").write_bytes(b"\x00\x00\x00\x18ftypavif" + b"x" * 8)
    (tmp_path / "0002.gif").write_bytes(b"GIF89a" + b"x" * 16)
    (tmp_path / "0002.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 16)

    pages = _downloaded_page_indices(str(tmp_path))
    assert pages == {0, 2}
    assert pages != set(range(3))
