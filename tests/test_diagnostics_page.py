import pytest


@pytest.fixture()
def page_client(monkeypatch):
    import app
    monkeypatch.setattr(app, "_auth_enabled", False)
    return app.app.test_client()


def test_diagnostics_page_and_main_navigation(page_client):
    response = page_client.get('/diagnostics')
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert '运行诊断 · webnovel_rules' in text
    assert 'id="report-download" type="button" disabled' in text
    assert 'readonly' in text and '/static/js/diagnostics.js' in text
    for path in ['/', '/sources', '/library', '/tasks_page', '/manga']:
        response = page_client.get(path)
        assert response.status_code == 200
        assert 'href="/diagnostics"' in response.get_data(as_text=True)


def test_diagnostics_page_inherits_login_protection(page_client, monkeypatch):
    import app
    monkeypatch.setattr(app, "_auth_enabled", True)
    response = page_client.get('/diagnostics')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/login')
    for path in ['/api/diagnostics/report', '/api/diagnostics/events']:
        response = page_client.get(path)
        assert response.status_code == 401
        assert response.get_json()['code'] == 'UNAUTHORIZED'
