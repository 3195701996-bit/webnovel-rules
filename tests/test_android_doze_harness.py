"""Host-controlled Doze rehearsal must fail closed before device mutation."""

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tools" / "android_doze_resilience.sh"


def _fake_adb(path: Path) -> Path:
    path.write_text(
        """#!/bin/sh
if [ "$1" = "-s" ]; then shift 2; fi
printf '%s\\n' "$*" >> "$MOCK_ADB_LOG"
case "$1:$2:$3" in
  get-state::) echo device ;;
  emu:avd:name) printf '%s\\nOK\\n' "$MOCK_AVD" ;;
  shell:getprop:ro.build.version.sdk) echo "$MOCK_SDK" ;;
  shell:getprop:ro.kernel.qemu) echo "$MOCK_QEMU" ;;
esac
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _run(tmp_path, *, serial="emulator-5554", avd="", sdk="35", qemu="1",
         doze_seconds="40"):
    log = tmp_path / "adb.log"
    adb = _fake_adb(tmp_path / "adb")
    env = os.environ.copy()
    env.update({
        "ANDROID_SERIAL": serial,
        "ADB": str(adb),
        "MOCK_ADB_LOG": str(log),
        "MOCK_AVD": avd,
        "MOCK_SDK": sdk,
        "MOCK_QEMU": qemu,
        "DOZE_SECONDS": doze_seconds,
    })
    result = subprocess.run(
        ["bash", str(HARNESS)], cwd=ROOT, env=env, text=True,
        capture_output=True, check=False, timeout=10,
    )
    return result, log.read_text(encoding="utf-8")


def test_doze_harness_requires_explicit_device_before_any_adb_call(tmp_path):
    log = tmp_path / "adb.log"
    adb = _fake_adb(tmp_path / "adb")
    env = os.environ.copy()
    env.update({"ADB": str(adb), "MOCK_ADB_LOG": str(log)})
    result = subprocess.run(
        ["bash", str(HARNESS)], cwd=ROOT, env=env, text=True,
        capture_output=True, check=False, timeout=10,
    )
    assert result.returncode == 2
    assert "必须显式指定 ANDROID_SERIAL" in result.stderr
    assert not log.exists()


def test_doze_harness_rejects_physical_device_before_install_or_idle(tmp_path):
    result, calls = _run(tmp_path, avd="", qemu="")
    assert result.returncode == 2
    assert "仅允许 Upgrade_API35/API 35 模拟器" in result.stderr
    assert "install" not in calls
    assert "force-idle" not in calls


def test_doze_harness_rejects_other_avd_before_install_or_idle(tmp_path):
    result, calls = _run(tmp_path, avd="Pixel_API35")
    assert result.returncode == 2
    assert "仅允许 Upgrade_API35/API 35 模拟器" in result.stderr
    assert "install" not in calls
    assert "force-idle" not in calls


def test_doze_harness_rejects_unknown_android_identity(tmp_path):
    result, calls = _run(tmp_path, avd="Upgrade_API35", sdk="", qemu="1")
    assert result.returncode == 2
    assert "仅允许 Upgrade_API35/API 35 模拟器" in result.stderr
    assert "install" not in calls
    assert "force-idle" not in calls


def test_doze_harness_rejects_invalid_observation_duration_before_install(tmp_path):
    result, calls = _run(tmp_path, avd="Upgrade_API35", doze_seconds="forever")
    assert result.returncode == 2
    assert "DOZE_SECONDS" in result.stderr
    assert "install" not in calls
    assert "force-idle" not in calls
