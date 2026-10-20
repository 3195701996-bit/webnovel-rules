"""Novel explore is a real navigable page, not a source-fetch side effect."""


def test_novel_explore_page_is_available_and_home_links_to_it(monkeypatch):
    import app
    from engine import explore

    def forbidden(*args, **kwargs):
        raise AssertionError("Rendering the page must not fetch source rankings")

    monkeypatch.setattr(explore, "explore", forbidden)
    client = app.app.test_client()
    response = client.get("/explore?source=literal%25id&category=%2Flist%252Fpart&page=2")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '<title>小说榜单 · webnovel_rules</title>' in html
    assert 'id="explore-menu"' in html
    assert 'id="explore-books"' in html
    assert '/static/js/novel_explore.js' in html
    assert 'href="/explore"' in client.get("/").get_data(as_text=True)
