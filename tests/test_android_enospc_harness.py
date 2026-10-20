"""The ENOSPC cold-start harness must target only the disposable stress AVD."""

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tools" / "android_enospc_cold_start.sh"


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
  shell:settings:get) echo "${MOCK_AIRPLANE:-0}" ;;
esac
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _run(tmp_path, *, serial="emulator-5580", avd="StorageStress_API35",
         sdk="35", qemu="1", offline="0", airplane="0"):
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
        "ENOSPC_OFFLINE_RECOVERY": offline,
        "MOCK_AIRPLANE": airplane,
    })
    result = subprocess.run(
        ["bash", str(HARNESS)], cwd=ROOT, env=env, text=True,
        capture_output=True, check=False, timeout=10,
    )
    return result, log.read_text(encoding="utf-8") if log.exists() else ""


def test_harness_requires_explicit_device_before_any_adb_call(tmp_path):
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


def test_harness_rejects_physical_device_before_install(tmp_path):
    result, calls = _run(tmp_path, avd="", qemu="")
    assert result.returncode == 2
    assert "仅允许 StorageStress_API35/API 35 模拟器" in result.stderr
    assert "install" not in calls
    assert "force-stop" not in calls


def test_harness_rejects_other_avd_before_install(tmp_path):
    result, calls = _run(tmp_path, avd="Upgrade_API35")
    assert result.returncode == 2
    assert "仅允许 StorageStress_API35/API 35 模拟器" in result.stderr
    assert "install" not in calls
    assert "force-stop" not in calls


def test_harness_rejects_wrong_android_version_before_install(tmp_path):
    result, calls = _run(tmp_path, sdk="34")
    assert result.returncode == 2
    assert "仅允许 StorageStress_API35/API 35 模拟器" in result.stderr
    assert "install" not in calls
    assert "force-stop" not in calls


def test_harness_keeps_real_force_stop_outside_instrumentation():
    source = (ROOT / "android" / "app" / "src" / "androidTest" / "java" /
              "com" / "webnovel" / "mobile" / "DownloadResilienceTest.kt")
    source = source.read_text(encoding="utf-8")
    script = HARNESS.read_text(encoding="utf-8")
    assert "fullStorageFailurePersistsForColdRestart" in source
    assert "persistedFullStorageFailureResumesAfterColdAppStart" in source
    assert "adb_cmd[@]}\" shell am force-stop com.webnovel.mobile" in script
    assert script.index("fullStorageFailurePersistsForColdRestart") < script.index(
        "shell am force-stop com.webnovel.mobile") < script.index(
            "persistedFullStorageFailureResumesAfterColdAppStart")
    assert 'getString("enospcHarness") == "1"' in source
    assert script.count("-e enospcHarness 1") == 2


def test_cold_recovery_requires_ui_and_persisted_completion_evidence():
    script = HARNESS.read_text(encoding="utf-8")
    for marker in ("enospcUiReason=PASS", "enospcUiResume=PASS",
                   "enospcCompleted=PASS done"):
        assert f'"INSTRUMENTATION_STATUS: {marker}"' in script
    source = (ROOT / "android/app/src/androidTest/java/com/webnovel/mobile/"
              "DownloadResilienceTest.kt").read_text(encoding="utf-8")
    recovery = source.split("fun persistedFullStorageFailureResumesAfterColdAppStart()", 1)[1]
    recovery = recovery.split("private suspend fun taskStatus", 1)[0]
    assert 'saved.optString("status") == "done"' in recovery
    assert 'images().size == total' in recovery
    assert 'hasAnyAncestor(hasTestTag("task_card_$key"))' in recovery
    assert 'performScrollTo().assertIsDisplayed()' in recovery
    assert '.assertIsEnabled().performClick()' in recovery
    # External network failure is a failed mandatory acceptance run, not evidence
    # of successful recovery. The entry guard may still skip ordinary test runs.
    after_resume = recovery.split('reportEvidence("enospcUiResume"', 1)[1]
    assert "assumeTrue" not in after_resume


def test_combined_fault_rejects_physical_device_without_network_changes(tmp_path):
    result, calls = _run(tmp_path, avd="", qemu="", offline="1")
    assert result.returncode == 2
    assert "airplane-mode" not in calls
    assert "install" not in calls


def test_combined_fault_rejects_invalid_option_before_adb(tmp_path):
    result, calls = _run(tmp_path, offline="yes")
    assert result.returncode == 2
    assert "仅允许 0 或 1" in result.stderr
    assert not calls


def test_combined_fault_requires_both_offline_and_recovery_evidence():
    script = HARNESS.read_text(encoding="utf-8")
    assert '-e enospcOfflineRecovery "$offline_recovery"' in script
    assert '"INSTRUMENTATION_STATUS: enospcOffline=PASS"' in script
    assert '"INSTRUMENTATION_STATUS: enospcOnline=PASS"' in script
    cleanup = script.split("cleanup() {", 1)[1].split("trap cleanup EXIT", 1)[0]
    assert '"${adb_cmd[@]}" shell cmd connectivity airplane-mode disable' in cleanup


def test_combined_fault_preserves_already_offline_device(tmp_path):
    result, calls = _run(tmp_path, offline="1", airplane="1")
    assert result.returncode == 2
    assert "初始飞行模式关闭" in result.stderr
    assert "airplane-mode disable" not in calls
    assert "install" not in calls


def test_pressure_and_recovery_have_instrumentation_device_guards():
    source = (ROOT / "android/app/src/androidTest/java/com/webnovel/mobile/"
              "DownloadResilienceTest.kt").read_text(encoding="utf-8")
    for method in ("fullStorageFailurePersistsForColdRestart",
                   "persistedFullStorageFailureResumesAfterColdAppStart"):
        body = source.split(f"fun {method}()", 1)[1]
        assert body.index("assertStorageStressEmulator()") < body.index("val ")
    assert 'Shell.run("getprop ro.boot.qemu.avd_name").trim() == "StorageStress_API35"' in source
