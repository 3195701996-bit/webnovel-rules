from io import BytesIO
from zipfile import ZipFile
from pathlib import Path
import subprocess
import sys
import pytest

from tools.check_android_web_assets import check_web_assets


def bundle(tmp_path, entries):
    inner = BytesIO()
    with ZipFile(inner, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    apk = tmp_path / "fixture.apk"
    with ZipFile(apk, "w") as archive:
        archive.writestr("assets/chaquopy/app.imy", inner.getvalue())
    return apk


def test_packaged_resources_match_source_bytes(tmp_path):
    (tmp_path / "static").mkdir()
    (tmp_path / "static/new.js").write_bytes(b"new")
    apk = bundle(tmp_path, [("static/new.js", b"new"), ("server/app.py", b"ignored")])
    assert check_web_assets(apk, tmp_path) == []


def test_packaged_resources_report_missing_stale_and_changed(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates/index.html").write_bytes(b"current")
    (tmp_path / "templates/added.html").write_bytes(b"added")
    apk = bundle(tmp_path, [("templates/index.html", b"old"), ("static/removed.js", b"stale")])
    assert check_web_assets(apk, tmp_path) == ["Missing: templates/added.html",
        "Obsolete: static/removed.js", "Content mismatch: templates/index.html"]


def test_empty_source_cannot_pass_accidentally(tmp_path):
    assert check_web_assets(tmp_path / "absent.apk", tmp_path) == ["No source Web resources found"]


def test_duplicate_archive_entries_are_rejected(tmp_path):
    (tmp_path / "static").mkdir()
    (tmp_path / "static/new.js").write_bytes(b"new")
    with pytest.warns(UserWarning, match="Duplicate name"):
        apk = bundle(tmp_path, [("static/new.js", b"new"), ("static/new.js", b"new")])
    assert check_web_assets(apk, tmp_path) == ["Duplicate: static/new.js"]


@pytest.mark.parametrize("failure", ["corrupt_apk", "missing_bundle", "corrupt_bundle", "duplicate_bundle"])
def test_cli_fails_closed_for_invalid_application_archives(tmp_path, failure):
    (tmp_path / "static").mkdir()
    (tmp_path / "static/new.js").write_bytes(b"new")
    apk = tmp_path / "fixture.apk"
    if failure == "corrupt_apk":
        apk.write_bytes(b"not an apk")
    else:
        with ZipFile(apk, "w") as archive:
            if failure == "corrupt_bundle":
                archive.writestr("assets/chaquopy/app.imy", b"not a bundle")
            elif failure == "duplicate_bundle":
                archive.writestr("assets/chaquopy/app.imy", b"first")
                with pytest.warns(UserWarning, match="Duplicate name"):
                    archive.writestr("assets/chaquopy/app.imy", b"last")
    tool = Path(__file__).resolve().parents[1] / "tools/check_android_web_assets.py"
    result = subprocess.run([sys.executable, str(tool), str(apk), "--root", str(tmp_path)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert "match source exactly" not in result.stdout
    assert result.stderr.strip()
    assert "Traceback" not in result.stderr
