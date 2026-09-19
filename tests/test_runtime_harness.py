"""Local HTTP checks must be isolated and own every contacted process."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from runtime_server import LocalServer
from verify_runtime_round import prepare_crash

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fd passing")


def test_owned_server_isolated_and_reaped(tmp_path, monkeypatch):
    monkeypatch.setenv("WR_AUTH_PASSWORD", "must-not-be-inherited")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    sources = tmp_path / "sources"
    sources.mkdir()
    data = tmp_path / "data"
    target, txn, backup, image = prepare_crash(data)
    with LocalServer(data, sources) as server:
        code, body, headers = server.request("GET", "/api/sources")
        assert code == 200
        assert json.loads(body)["sources"] == []
        assert headers["X-WR-Test-Instance"] == server.token
        assert target.is_dir() and not txn.exists() and not backup.exists()
        assert all((target / f"{i:04d}.webp").read_bytes() == image for i in range(3))
        code, _, _ = server.request("POST", "/api/books/local/progress",
                                    b'{"idx":1,"pct":10}')
        assert code == 200
    assert server.process.poll() is not None
    assert server.log is None


def test_owned_server_reaped_when_check_raises(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    server = LocalServer(tmp_path / "data", sources)
    with pytest.raises(RuntimeError, match="check failure"):
        with server:
            raise RuntimeError("check failure")
    assert server.process.poll() is not None
    assert server.log is None
