#!/usr/bin/env bash
set -euo pipefail

# Two-process real-storage-pressure acceptance. The host (not instrumentation)
# force-stops the app between the ENOSPC write and cold-start recovery phases.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
android_dir="$repo_root/android"
adb_bin="${ADB:-adb}"
gradlew="${GRADLEW:-./gradlew}"
if [[ -z "${ANDROID_SERIAL:-}" ]]; then
  echo "必须显式指定 ANDROID_SERIAL=emulator-5580；拒绝在未指定设备时继续。" >&2
  exit 2
fi

adb_cmd=("$adb_bin" -s "$ANDROID_SERIAL")
if ! "${adb_cmd[@]}" get-state >/dev/null 2>&1; then
  echo "指定设备不可用；未安装或更改任何设备状态。" >&2
  exit 2
fi
avd_name="$("${adb_cmd[@]}" emu avd name 2>/dev/null | sed -n '1{s/\r$//;p;}')"
sdk_level="$("${adb_cmd[@]}" shell getprop ro.build.version.sdk 2>/dev/null | tr -d '\r')"
is_emulator="$("${adb_cmd[@]}" shell getprop ro.kernel.qemu 2>/dev/null | tr -d '\r')"
if [[ "$avd_name" != "StorageStress_API35" || "$sdk_level" != "35" || "$is_emulator" != "1" ]]; then
  echo "拒绝低存储测试：仅允许 StorageStress_API35/API 35 模拟器 " \
       "(当前 AVD='${avd_name:-未知}' API='${sdk_level:-未知}' emulator='${is_emulator:-未知}')。" >&2
  exit 2
fi

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/wr-enospc.XXXXXX")"
phase1_log="$tmp_dir/phase1.log"
phase2_log="$tmp_dir/phase2.log"
test_runner="com.webnovel.mobile.test/androidx.test.runner.AndroidJUnitRunner"
phase2_ok=0
cleanup() {
  local rc=$?
  if [[ "$rc" == 0 && "$phase2_ok" == 1 ]]; then
    rm -rf "$tmp_dir"
  else
    echo "ENOSPC 冷启动演练未完整通过；诊断日志保留于：$tmp_dir" >&2
  fi
}
trap cleanup EXIT

echo "[1/6] 构建 1.6.4 debug 验证包（不发布）"
cd "$android_dir"
"$gradlew" :app:testDebugUnitTest :app:compileDebugAndroidTestKotlin \
  :app:assembleDebug :app:assembleDebugAndroidTest
"${adb_cmd[@]}" install -r app/build/outputs/apk/debug/app-debug.apk
"${adb_cmd[@]}" install -r app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk

echo "[2/6] 真实低存储下载：观察 ENOSPC 错误与任务快照落盘"
if ! "${adb_cmd[@]}" shell am instrument -w -r \
  -e enospcHarness 1 \
  -e class "com.webnovel.mobile.DownloadResilienceTest#fullStorageFailurePersistsForColdRestart" \
  "$test_runner" >"$phase1_log" 2>&1; then
  cat "$phase1_log" >&2
  exit 1
fi
for evidence in "INSTRUMENTATION_STATUS: enospcLowSpace=READY" \
                "INSTRUMENTATION_STATUS: enospcPersisted=PASS" \
                "INSTRUMENTATION_STATUS: enospcPhase1=READY" "OK (1 test)"; do
  if ! grep -Fq "$evidence" "$phase1_log"; then
    cat "$phase1_log" >&2
    echo "阶段 1 缺少证据：$evidence" >&2
    exit 1
  fi
done
cat "$phase1_log"

echo "[3/6] 核对任务快照，仅对指定测试 AVD 强杀 App 进程"
"${adb_cmd[@]}" shell run-as com.webnovel.mobile grep -Fq \
  __download_resilience_ files/runtime/manga/_tasks.json
"${adb_cmd[@]}" shell am force-stop com.webnovel.mobile
pid=""
for _ in $(seq 1 20); do
  pid="$("${adb_cmd[@]}" shell pidof com.webnovel.mobile 2>/dev/null | tr -d '\r' || true)"
  [[ -z "$pid" ]] && break
  sleep 1
done
if [[ -n "$pid" ]]; then
  echo "强制停止后 App 进程仍存在（PID=$pid）；拒绝声称发生冷启动。" >&2
  exit 1
fi
echo "已确认旧 App 进程退出。"

echo "[4/6] 独立仪器阶段冷启动引擎，恢复任务并继续下载"
if ! "${adb_cmd[@]}" shell am instrument -w -r \
  -e enospcHarness 1 \
  -e class "com.webnovel.mobile.DownloadResilienceTest#persistedFullStorageFailureResumesAfterColdAppStart" \
  "$test_runner" >"$phase2_log" 2>&1; then
  cat "$phase2_log" >&2
  exit 1
fi
for evidence in "INSTRUMENTATION_STATUS: enospcRecovered=PASS error" \
                "INSTRUMENTATION_STATUS: enospcResumed=PASS" "OK (1 test)"; do
  if ! grep -Fq "$evidence" "$phase2_log"; then
    cat "$phase2_log" >&2
    echo "阶段 2 缺少证据：$evidence" >&2
    exit 1
  fi
done
phase2_ok=1
cat "$phase2_log"

echo "[5/6] 确认本轮 synthetic 任务与压力文件已清理"
if "${adb_cmd[@]}" shell run-as com.webnovel.mobile grep -Fq \
  __download_resilience_ files/runtime/manga/_tasks.json; then
  echo "任务快照仍有本轮测试身份；没有触碰其他漫画记录。" >&2
  exit 1
fi
"${adb_cmd[@]}" shell run-as com.webnovel.mobile test \
  ! -e files/runtime/manga/downloads/mangadex/__storage_pressure__/filler.bin
"${adb_cmd[@]}" shell df -h /data

echo "[6/6] PASS：真实 ENOSPC → 错误状态落盘 → 宿主强杀 → 冷启动 → 同任务续传。"
