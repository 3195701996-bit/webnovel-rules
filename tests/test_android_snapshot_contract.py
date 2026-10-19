"""Build snapshot must mirror the source whitelist, not accumulate stale resources."""
from pathlib import Path


def test_android_python_snapshot_is_a_whitelisted_sync():
    root = Path(__file__).resolve().parents[1]
    gradle = (root / "android/app/build.gradle").read_text(encoding="utf-8")
    snapshot = gradle.split("tasks.register('syncPythonSnapshot', Sync) {", 1)[1]
    snapshot = snapshot.split("// 内置书源", 1)[0]
    for pattern in ("app.py", "server/**", "engine/**", "templates/**", "static/**"):
        assert f"include '{pattern}'" in snapshot
    assert "into snapshotDir" in snapshot
    assert "exclude '**/__pycache__/**'" in snapshot
    assert "exclude '**/*.pyc'" in snapshot
