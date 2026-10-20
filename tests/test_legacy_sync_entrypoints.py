import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "android").mkdir()
    (tmp_path / "tools").mkdir()
    for name in ("sync_android.sh", "sync_templates.sh"):
        shutil.copy2(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    gradle = tmp_path / "android" / "gradlew"
    gradle.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    gradle.chmod(0o755)
    (tmp_path / "tools" / "check_android_web_assets.py").write_text(
        'import sys\nprint("CHECK " + repr(sys.argv[1:]))\n')
    return tmp_path


def run(repo, name="sync_android.sh", *args):
    return subprocess.run(["bash", str(repo / "scripts" / name), *args],
                          cwd=repo, env=dict(os.environ, PYTHON=sys.executable),
                          capture_output=True, text=True, timeout=5)


@pytest.mark.parametrize("name", ["sync_android.sh", "sync_templates.sh"])
def test_default_uses_gradle_snapshot_without_obsolete_directory(repo, name):
    result = run(repo, name)
    assert result.returncode == 0, result.stderr
    assert ":app:syncPythonSnapshot" in result.stdout
    assert "--no-daemon" in result.stdout
    assert str(repo / "android") in result.stdout
    assert not (repo / "android_app").exists()


@pytest.mark.parametrize("name", ["sync_android.sh", "sync_templates.sh"])
def test_check_passes_one_literal_apk_path(repo, name):
    result = run(repo, name, "--check", "APK with spaces.apk")
    assert result.returncode == 0, result.stderr
    assert "CHECK ['APK with spaces.apk']" in result.stdout
    assert ":app:syncPythonSnapshot" not in result.stdout
    assert not (repo / "android_app").exists()


def test_check_without_explicit_apk_is_not_fake_success(repo):
    result = run(repo, "sync_android.sh", "--check")
    assert result.returncode == 2
    assert "用法" in result.stderr


def test_help_does_not_start_build(repo):
    result = run(repo, "sync_android.sh", "--help")
    assert result.returncode == 0
    assert "用法" in result.stdout
    assert "CHECK [" not in result.stdout


def test_unknown_arguments_fail(repo):
    assert run(repo, "sync_android.sh", "--unknown").returncode == 2
