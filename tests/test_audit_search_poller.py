"""Execute production JS with controlled responses, and check changed templates."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")


def test_search_poller_behaviour():
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/audit_search_poller.test.js")],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL ASSERTIONS PASSED" in result.stdout


@pytest.mark.parametrize("name", [
    "sources", "manga", "manga_detail", "manga_reader", "library",
    "tasks", "manga_download",
])
def test_changed_template_syntax(name, tmp_path):
    html = (ROOT / "templates" / f"{name}.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
    assert scripts
    for i, script in enumerate(scripts):
        code = re.sub(r"\{\{.*?\}\}", '"test"', script)
        dest = tmp_path / f"{name}_{i}.js"
        dest.write_text(code, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(dest)],
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
