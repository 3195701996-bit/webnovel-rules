"""Management inventories must not weaken the complete-only local reader."""
import json

import pytest


@pytest.fixture
def media_env(tmp_path, monkeypatch):
    import app
    import server.manga_api as api
    import server.state as state
    downloads, cache = tmp_path / "downloads", tmp_path / "cache"
    for module in (api, state):
        monkeypatch.setattr(module, "MANGA_DOWNLOADS_DIR", str(downloads))
        monkeypatch.setattr(module, "MANGA_CACHE_DIR", str(cache))
    def no_adapter(*args, **kwargs):
        raise AssertionError("local media management must not call adapters")
    monkeypatch.setattr(api, "_manga_adapter", no_adapter)
    monkeypatch.setattr(api, "_manga_stats_request", lambda *args: None)
    return app.app.test_client(), downloads, cache


def page(path):
    path.mkdir(parents=True)
    (path / "0000.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 2048)


def test_inventory_includes_partial_volume_without_promoting_it(media_env):
    client, downloads, _ = media_env
    base = downloads / "mangadex" / "local-inventory"
    page(base / "partial-volume")
    page(base / "complete-chapter")
    manifest = base / "_info.json"
    manifest.write_text(json.dumps({"chapters": [
        {"id": "complete-chapter", "name": "第02话", "download_page_count": 1},
        {"id": "partial-volume", "name": "第01卷", "download_page_count": 3},
        {"id": "not-downloaded", "name": "第03话"},
    ]}), encoding="utf-8")
    before = {p: p.read_bytes() for p in base.rglob("*") if p.is_file()}
    response = client.get("/api/manga/mangadex/local-inventory/local-media")
    assert response.status_code == 200
    result = response.get_json()
    assert result["ok"] and result["local_only"] and result["count"] == 2
    assert result["media"] == [
        {"id": "partial-volume", "name": "第01卷", "group": "", "downloaded": False, "partial": True},
        {"id": "complete-chapter", "name": "第02话", "group": "", "downloaded": True, "partial": False},
    ]
    assert {p: p.read_bytes() for p in base.rglob("*") if p.is_file()} == before
    import server.state as state
    assert "partial-volume" not in state._scan_downloaded_chapters("mangadex", "local-inventory")


def test_inventory_excludes_read_only_cache_and_preserves_same_name_ids(media_env):
    client, downloads, cache = media_env
    base = downloads / "copymanga" / "same-name"
    for chapter_id in ("old-id", "new-id"):
        page(base / chapter_id)
    (base / "_info.json").write_text(json.dumps({"chapters": [
        {"id": "old-id", "name": "第41話", "local_archive": True},
        {"id": "new-id", "name": "第41話"},
    ]}), encoding="utf-8")
    page(cache / "copymanga" / "same-name" / "read-only-cache")
    result = client.get("/api/manga/copymanga/same-name/local-media").get_json()
    assert {row["id"] for row in result["media"]} == {"old-id", "new-id"}
    assert all(row["downloaded"] and not row["partial"] for row in result["media"])


def test_empty_inventory_stays_empty_without_network(media_env):
    client, _, _ = media_env
    result = client.get("/api/manga/mangadex/no-media/local-media")
    assert result.status_code == 200
    assert result.get_json()["media"] == []
    assert result.get_json()["count"] == 0


def test_legacy_alias_media_is_visible_with_exact_disk_identity(media_env):
    client, downloads, _ = media_env
    base = downloads / "copymanga_web" / "legacy-local"
    page(base / "legacy-uuid")
    (base / "_info.json").write_text(json.dumps({"chapters": [
        {"id": "legacy-uuid", "name": "第09话"},
    ]}), encoding="utf-8")
    result = client.get("/api/manga/copymanga/legacy-local/local-media").get_json()
    assert result["media"] == [
        {"id": "legacy-uuid", "name": "第09话", "group": "", "downloaded": True, "partial": False},
    ]


def test_broken_manifest_does_not_hide_proven_download_files(media_env):
    client, downloads, _ = media_env
    base = downloads / "mangadex" / "broken-manifest"
    page(base / "known-disk-id")
    (base / "_info.json").write_text("{broken", encoding="utf-8")
    result = client.get("/api/manga/mangadex/broken-manifest/local-media").get_json()
    assert result["media"][0]["id"] == "known-disk-id"
    assert result["media"][0]["name"] == "known-disk-id"
    assert result["media"][0]["downloaded"]
    assert "path" not in result["media"][0]


@pytest.mark.parametrize("selected,kept", [("old-id", "new-id"), ("new-id", "old-id")])
def test_exact_disk_delete_never_expands_by_same_name_across_alias_roots(media_env, selected, kept):
    client, downloads, _ = media_env
    paths = {}
    for source, chapter_id in (("copymanga", "old-id"), ("copymanga_web", "new-id")):
        base = downloads / source / "twins"
        paths[chapter_id] = base / chapter_id
        page(paths[chapter_id])
        (base / "_info.json").write_text(json.dumps({"chapters": [
            {"id": chapter_id, "name": "第41話", "local_archive": chapter_id == "old-id"},
        ]}), encoding="utf-8")
    kept_page = paths[kept] / "0000.jpg"
    before = kept_page.read_bytes()
    response = client.post("/api/manga/copymanga/twins/chapters/delete",
                           json={"chapter_ids": [selected], "identity_mode": "disk"})
    assert response.status_code == 200
    assert response.get_json()["identity_mode"] == "disk"
    assert response.get_json()["deleted"] == 1
    assert response.get_json()["missing"] == 0
    assert not paths[selected].exists()
    assert kept_page.read_bytes() == before


@pytest.mark.parametrize("body", [
    {"chapter_ids": ["one"], "identity_mode": "typo"},
    {"chapter_ids": "one", "identity_mode": "disk"},
    {"chapter_ids": [123], "identity_mode": "disk"},
    ["one"],
])
def test_invalid_exact_delete_parameters_preserve_media(media_env, body):
    client, downloads, _ = media_env
    directory = downloads / "mangadex" / "invalid-delete" / "one"
    page(directory)
    before = (directory / "0000.jpg").read_bytes()
    response = client.post("/api/manga/mangadex/invalid-delete/chapters/delete", json=body)
    assert response.status_code == 400
    assert (directory / "0000.jpg").read_bytes() == before


@pytest.mark.parametrize("mode,deleted", [("disk", 0), ("compatible", 1)])
def test_missing_disk_id_does_not_fall_back_but_legacy_mode_remains_compatible(media_env, mode, deleted):
    client, downloads, cache = media_env
    base = downloads / "copymanga_web" / "renamed-id"
    page(base / "legacy-id")
    (base / "_info.json").write_text(json.dumps({"chapters": [
        {"id": "legacy-id", "name": "第42話"},
    ]}), encoding="utf-8")
    current = cache / "copymanga" / "renamed-id"
    current.mkdir(parents=True)
    (current / "_info_full.json").write_text(json.dumps({"data": {"chapters": [
        {"id": "new-id", "name": "第42話"},
    ]}}), encoding="utf-8")
    response = client.post("/api/manga/copymanga/renamed-id/chapters/delete",
                           json={"chapter_ids": ["new-id"], "identity_mode": mode})
    assert response.status_code == 200
    assert response.get_json()["deleted"] == deleted
    assert response.get_json()["missing"] == (1 if mode == "disk" else 0)
    assert (base / "legacy-id").exists() == (mode == "disk")
