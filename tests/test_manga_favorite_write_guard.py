import json

import pytest


@pytest.mark.parametrize("content", [[{"legacy": "保留的未知结构"}], "原始收藏", None, 7])
@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_wrong_shape_favorites_are_not_replaced(tmp_path, monkeypatch, content, method):
    import app
    import server.manga_api as api
    favorite = tmp_path / "favorites.json"
    favorite.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    original = favorite.read_bytes()
    monkeypatch.setattr(api, "MANGA_FAV_FILE", str(favorite))
    client = app.app.test_client()
    if method == "POST":
        response = client.post("/api/manga/favorites", json={"source": "mangadex", "comic_id": "guard"})
    else:
        response = client.delete("/api/manga/favorites/mangadex/guard")
    assert response.status_code == 500
    assert response.get_json()["error"]
    assert favorite.read_bytes() == original


@pytest.mark.parametrize("payload", [[1], "invalid", 42, {"source": ["mangadex"], "comic_id": "one"},
                                      {"source": "mangadex", "comic_id": {"id": "one"}}])
def test_invalid_favorite_request_is_rejected_without_writing(tmp_path, monkeypatch, payload):
    import app
    import server.manga_api as api
    favorite = tmp_path / "favorites.json"
    monkeypatch.setattr(api, "MANGA_FAV_FILE", str(favorite))
    response = app.app.test_client().post("/api/manga/favorites", json=payload)
    assert response.status_code == 400
    assert not favorite.exists()


def test_download_reports_auto_favorite_failure_without_overwriting_bad_store(tmp_path, monkeypatch):
    import app
    import server.manga_api as api
    favorite = tmp_path / "favorites.json"
    original = b'["keep-original"]'
    favorite.write_bytes(original)
    monkeypatch.setattr(api, "MANGA_FAV_FILE", str(favorite))
    starts = []
    def start(*args, **kwargs):
        starts.append((args, kwargs))
        return "mangadex:one", "queued"
    monkeypatch.setattr(api._manga_dl, "start", start)
    response = app.app.test_client().post("/api/manga/mangadex/one/download", json={"chapters": ["chapter-1"]})
    assert response.status_code == 200
    result = response.get_json()
    assert result["ok"] and result["status"] == "queued"
    assert result["favorited"] is False and result["favorite_warning"]
    assert len(starts) == 1 and starts[0][1]["chapters"] == ["chapter-1"]
    assert favorite.read_bytes() == original
