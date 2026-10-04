"""Exercise Android upgrade harness safety and EXIT cleanup with fake tools."""

import os
import subprocess
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tools" / "android_upgrade_install_harness.sh"
UPGRADE_TEST = REPO / "android" / "app" / "src" / "androidTest" / "java" / \
    "com" / "webnovel" / "mobile" / "UpgradeConsistencyTest.kt"
EVIDENCE = "UPGRADE_EVIDENCE 失败后夹具下载/小说/书目/收藏/历史及用户书源已清理或还原"


def _executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fixture(tmp_path, *, avd="ReleaseUpgrade_API35", sdk="35", installed="",
             candidate_dirty="clean", test_revision=None, test_dirty="clean",
             test_name=None, test_code=None, old_name="1.6.4", old_code="154",
             new_name="2.0.0", new_code="200"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    apks = tuple(tmp_path / name for name in ("old.apk", "new.apk", "test.apk"))
    revision = "b" * 40
    for apk in apks:
        with zipfile.ZipFile(apk, "w") as archive:
            if apk.name == "new.apk":
                archive.writestr("assets/build-identity.properties", (
                    f"revision={revision}\ndirty={candidate_dirty}\n"
                    f"versionName={new_name}\nversionCode={new_code}\n"
                ))
            elif apk.name == "test.apk":
                archive.writestr("assets/build-identity.properties", (
                    f"revision={test_revision or revision}\ndirty={test_dirty}\n"
                    f"versionName={test_name or new_name}\n"
                    f"versionCode={test_code or new_code}\n"
                ))

    adb_log = tmp_path / "adb.log"
    installed_state = tmp_path / "old-installed"
    tool_dir = tmp_path / "android tools"
    tool_dir.mkdir()
    adb = _executable(tool_dir / "adb", """#!/bin/sh
if [ "$1" = "-s" ]; then shift 2; fi
printf '%s\\n' "$*" >> "$MOCK_ADB_LOG"
case "$1" in
  get-state) echo device ;;
  emu)
    if [ "$2" = "avd" ] && [ "$3" = "name" ]; then
      [ -z "$MOCK_AVD" ] || echo "$MOCK_AVD"
      echo OK
    fi
    ;;
  install)
    if [ "$(basename "$3")" = "old.apk" ]; then touch "$MOCK_INSTALLED_STATE"; fi
    ;;
      shell)
    case "$2" in
      logcat) ;;
      getprop)
        case "$3" in
          ro.build.version.sdk) echo "$MOCK_SDK" ;;
          ro.kernel.qemu) echo 1 ;;
        esac
        ;;
      dumpsys)
        if [ -f "$MOCK_INSTALLED_STATE" ]; then
          if [ "$MOCK_VERBOSE_DUMPSYS" = "1" ]; then
            i=0
            while [ "$i" -lt 12000 ]; do
              echo "package line $i versionCode=$MOCK_OLD_CODE minSdk=24 targetSdk=35"
              i=$((i + 1))
            done
          else
            echo "versionCode=$MOCK_OLD_CODE"
          fi
        fi
        ;;
      pm)
        if [ "$3" = "list" ] && [ "$4" = "packages" ]; then
          if [ "$MOCK_PREINSTALLED" = "target" ]; then echo "package:$5"; fi
          if [ "$MOCK_PREINSTALLED" = "test-only" ]; then echo 'package:com.webnovel.mobile.test'; fi
        elif [ "$3" = "am" ]; then
          case "$*" in
            *phase1_setupState*) echo 'phase 1 intentionally missing completion evidence' ;;
            *cleanupAfterFailure*) echo 'UPGRADE_EVIDENCE 失败后夹具下载/小说/书目/收藏/历史及用户书源已清理或还原' ;;
          esac
        fi
        ;;
      am)
        case "$*" in
          *phase1_setupState*) echo 'phase 1 intentionally missing completion evidence' ;;
          *cleanupAfterFailure*) echo 'UPGRADE_EVIDENCE 失败后夹具下载/小说/书目/收藏/历史及用户书源已清理或还原' ;;
        esac
        ;;
    esac
    ;;
esac
exit 0
""")
    apkanalyzer = _executable(tool_dir / "apkanalyzer", """#!/bin/sh
case "$2:$(basename "$3")" in
  application-id:old.apk|application-id:new.apk) echo com.webnovel.mobile ;;
  application-id:test.apk) echo com.webnovel.mobile.test ;;
  version-code:old.apk) echo "$MOCK_OLD_CODE" ;;
  version-code:new.apk) echo "$MOCK_NEW_CODE" ;;
  version-name:old.apk) echo "$MOCK_OLD_NAME" ;;
  version-name:new.apk) echo "$MOCK_NEW_NAME" ;;
  *) exit 2 ;;
esac
""")
    cert = "a" * 64
    apksigner = _executable(tool_dir / "apksigner", f"""#!/bin/sh
echo 'Signer #1 certificate SHA-256 digest: {cert}'
""")

    env = os.environ.copy()
    env.update({
        "ANDROID_SERIAL": "emulator-5554",
        "ADB": str(adb),
        "APKANALYZER": str(apkanalyzer),
        "APKSIGNER": str(apksigner),
        "MOCK_ADB_LOG": str(adb_log),
        "MOCK_INSTALLED_STATE": str(installed_state),
        "MOCK_AVD": avd,
        "MOCK_SDK": sdk,
        "MOCK_PREINSTALLED": installed,
        "MOCK_OLD_CODE": old_code,
        "MOCK_NEW_CODE": new_code,
        "MOCK_OLD_NAME": old_name,
        "MOCK_NEW_NAME": new_name,
    })
    return apks, adb_log, env


def _run(apks, env):
    return subprocess.run(
        [str(HARNESS), *(str(apk) for apk in apks)],
        cwd=REPO, env=env, text=True, capture_output=True, check=False,
    )


def test_upgrade_data_audit_rejects_missing_task_and_checks_exact_identities():
    source = UPGRADE_TEST.read_text(encoding="utf-8")

    assert 'status in listOf("paused", "stopped")' in source
    assert 'status in listOf("paused", "stopped", "idle")' not in source
    assert 'initialStatus in listOf("paused", "stopped")' in source
    assert 'taskState.optString("stop_kind")' in source
    assert 'stableTask.optString("stop_kind")' in source
    assert '"user_pause"' in source
    assert 'it.optString("source") == "mangadex"' in source
    assert 'it.optString("identity_source") == "mangadex"' in source
    assert '"comic_id") == e.getString("fixture_comic")' in source
    assert '"read_chapter_ids"' in source
    assert 'SelfTestComic.CH2_ID' in source
    assert 'SelfTestBook(key = novelKey)' in source
    assert '"/api/books/$novelKeyAfterUpgrade/chapter/1"' in source
    assert 'novel_cache_sha256' in source
    assert 'novel_progress_pct' in source
    assert 'SelfTestBook(key = cleanupNovel).cleanupFixtureOnly()' in source

    harness = HARNESS.read_text(encoding="utf-8")
    assert 'assets/build-identity.properties' in harness
    assert 'test_revision" == "$new_revision"' in harness
    assert 'test_identity_code" == "$new_code"' in harness
    assert 'shell am start -n "${package}/.MainActivity"' in harness
    assert 'logcat -c' in harness
    assert 'logcat -d -s System.out:I' in harness


def test_strict_upgrade_fixture_never_falls_back_to_another_source():
    fixture = (REPO / "android" / "app" / "src" / "androidTest" / "java" /
               "com" / "webnovel" / "mobile" / "MangaFixture.kt").read_text(
                   encoding="utf-8")

    assert 'cached?.takeIf { !requireReachable || it.source == source }' in fixture
    assert 'if (requireReachable) listOf(source)' in fixture
    assert 'source == "mangadex"' in UPGRADE_TEST.read_text(encoding="utf-8")
    assert 'preferredChapterId: String = ""' in fixture


def test_phase1_failure_runs_cleanup_and_preserves_failure_logs(tmp_path):
    apks, adb_log, env = _fixture(tmp_path)
    result = _run(apks, env)

    assert result.returncode != 0
    assert "未产生明确完成证据" in result.stderr
    assert "失败后的夹具清理与源文件恢复完成" in result.stderr
    adb_calls = adb_log.read_text(encoding="utf-8")
    assert "phase1_setupState" in adb_calls
    assert "cleanupAfterFailure" in adb_calls
    assert "install -r " + str(apks[1]) not in adb_calls
    assert "升级验证日志保留在：" in result.stderr


def test_rejects_non_target_avd_and_api_before_install(tmp_path):
    apks, adb_log, env = _fixture(tmp_path, avd="Pixel_API32", sdk="32")
    result = _run(apks, env)

    assert result.returncode == 2
    assert "仅允许指定的 ReleaseUpgrade_API35/API 35 模拟器" in result.stderr
    assert "未安装/覆盖任何包" in result.stderr
    assert "install " not in adb_log.read_text(encoding="utf-8")


def test_rejects_unknown_target_identity_before_install(tmp_path):
    apks, adb_log, env = _fixture(tmp_path, avd="", sdk="")
    result = _run(apks, env)

    assert result.returncode == 2
    assert "仅允许指定的 ReleaseUpgrade_API35/API 35 模拟器" in result.stderr
    assert "install " not in adb_log.read_text(encoding="utf-8")


def test_rejects_already_installed_app_before_install(tmp_path):
    apks, adb_log, env = _fixture(tmp_path, installed="target")
    result = _run(apks, env)

    assert result.returncode == 2
    assert "目标设备已安装 com.webnovel.mobile" in result.stderr
    assert "未安装/覆盖任何包" in result.stderr
    assert "install " not in adb_log.read_text(encoding="utf-8")


def test_release_upgrade_avd_is_the_safe_default(tmp_path):
    apks, adb_log, env = _fixture(tmp_path, avd="ReleaseUpgrade_API35")
    result = _run(apks, env)

    # Fake instrumentation deliberately omits phase-one evidence, so this
    # proves validation reached the isolated AVD while still failing closed.
    assert result.returncode != 0
    assert "目标设备：emulator-5554（ReleaseUpgrade_API35/API 35）" in result.stdout
    calls = adb_log.read_text(encoding="utf-8")
    assert "install -r " + str(apks[0]) in calls
    assert "phase1_setupState" in calls
    assert "install -r " + str(apks[1]) not in calls


def test_explicitly_configured_v200_to_v201_upgrade_is_supported(tmp_path):
    apks, adb_log, env = _fixture(
        tmp_path, old_name="2.0.0", old_code="155", new_name="2.0.1", new_code="156")
    env.update({
        "UPGRADE_OLD_VERSION_NAME": "2.0.0",
        "UPGRADE_OLD_VERSION_CODE": "155",
        "UPGRADE_NEW_VERSION_NAME": "2.0.1",
        "MOCK_VERBOSE_DUMPSYS": "1",
    })
    result = _run(apks, env)

    assert result.returncode != 0  # Fake instrumentation deliberately omits phase-1 proof.
    assert "旧版基线必须是" not in result.stderr
    assert "phase 1 intentionally missing completion evidence" in result.stdout
    calls = adb_log.read_text(encoding="utf-8")
    assert "install -r " + str(apks[0]) in calls
    assert "shell am start -n com.webnovel.mobile/.MainActivity" in calls
    assert "phase1_setupState" in calls
    assert "install -r " + str(apks[1]) not in calls


def test_explicit_upgrade_avd_override_remains_supported(tmp_path):
    apks, adb_log, env = _fixture(tmp_path, avd="CustomSafe_API35")
    env["UPGRADE_AVD_NAME"] = "CustomSafe_API35"
    result = _run(apks, env)

    assert result.returncode != 0  # Fixture intentionally omits phase-1 evidence.
    assert "目标设备：emulator-5554（CustomSafe_API35/API 35）" in result.stdout
    calls = adb_log.read_text(encoding="utf-8")
    assert "install -r " + str(apks[0]) in calls
    assert "phase1_setupState" in calls
    assert "install -r " + str(apks[1]) not in calls


def test_test_package_prefix_does_not_masquerade_as_target_app(tmp_path):
    apks, adb_log, env = _fixture(tmp_path, installed="test-only")
    result = _run(apks, env)

    assert result.returncode != 0
    assert "目标设备已安装 com.webnovel.mobile" not in result.stderr
    assert "phase1_setupState" in adb_log.read_text(encoding="utf-8")


def test_rejects_instrumentation_from_different_revision_before_device_changes(tmp_path):
    apks, adb_log, env = _fixture(tmp_path, test_revision="c" * 40)
    result = _run(apks, env)

    assert result.returncode == 2
    assert "instrumentation APK 必须与新版 APK 使用同一 clean revision 和版本" in result.stderr
    assert "install " not in adb_log.read_text(encoding="utf-8")


def test_rejects_dirty_or_wrong_version_instrumentation_before_device_changes(tmp_path):
    dirty_apks, dirty_log, dirty_env = _fixture(tmp_path / "dirty", test_dirty="1 files")
    dirty_result = _run(dirty_apks, dirty_env)
    assert dirty_result.returncode == 2
    assert "同一 clean revision 和版本" in dirty_result.stderr
    assert "install " not in dirty_log.read_text(encoding="utf-8")

    version_apks, version_log, version_env = _fixture(
        tmp_path / "version", test_name="1.6.4", test_code="154")
    version_result = _run(version_apks, version_env)
    assert version_result.returncode == 2
    assert "同一 clean revision 和版本" in version_result.stderr
    assert "install " not in version_log.read_text(encoding="utf-8")


def test_rejects_dirty_release_candidate_before_device_changes(tmp_path):
    apks, adb_log, env = _fixture(tmp_path, candidate_dirty="2 files")
    result = _run(apks, env)

    assert result.returncode == 2
    assert "新版 APK 必须来自完整 Git SHA 且源码状态为 clean" in result.stderr
    assert "install " not in adb_log.read_text(encoding="utf-8")
