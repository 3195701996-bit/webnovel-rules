#!/usr/bin/env bash
set -euo pipefail

# Real two-stage Android process-kill test. Device selection is mandatory: Gradle's
# connected-install task enumerates every attached device and can touch a daily phone.
# Build artifacts with Gradle, then install only via adb -s ANDROID_SERIAL.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
android_dir="$repo_root/android"
adb_bin="${ADB:-adb}"
if [[ -z "${ANDROID_SERIAL:-}" ]]; then
  echo "必须显式指定 ANDROID_SERIAL=<专用模拟器序列号>；为避免安装到日常手机，拒绝继续。" >&2
  exit 2
fi
test_class="${FORCE_STOP_TEST_CLASS:-com.webnovel.mobile.ForceStopResilienceTest}"
phase1_test="${FORCE_STOP_PHASE1_TEST:-phase1_startDownloadAndRecord}"
phase2_test="${FORCE_STOP_PHASE2_TEST:-phase2_verifyAfterForceStop_andResume}"
test_runner="com.webnovel.mobile.test/androidx.test.runner.AndroidJUnitRunner"
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/wr-force-stop.XXXXXX")"
phase1_log="$tmp_dir/phase1.log"
phase1_pid=""

cleanup() {
  local rc=$?
  if [[ -n "$phase1_pid" ]] && kill -0 "$phase1_pid" 2>/dev/null; then
    kill "$phase1_pid" 2>/dev/null || true
    wait "$phase1_pid" 2>/dev/null || true
  fi
  if [[ "$rc" -eq 0 ]]; then
    rm -rf "$tmp_dir"
  else
    echo "诊断输出已保留：$phase1_log" >&2
  fi
}
trap cleanup EXIT

adb_cmd=("$adb_bin" -s "$ANDROID_SERIAL")

if ! "${adb_cmd[@]}" get-state >/dev/null 2>&1; then
  echo "未检测到可用 Android 设备/模拟器；请连接设备并确认 adb 授权。" >&2
  exit 2
fi

# This test installs an APK and force-stops its package. Require the explicitly
# disposable AVD, not merely an explicitly named serial (which could be a phone).
avd_name="$("${adb_cmd[@]}" emu avd name 2>/dev/null | sed -n '1{s/\r$//;p;}')"
sdk_level="$("${adb_cmd[@]}" shell getprop ro.build.version.sdk 2>/dev/null | tr -d '\r')"
is_emulator="$("${adb_cmd[@]}" shell getprop ro.kernel.qemu 2>/dev/null | tr -d '\r')"
if [[ "$avd_name" != "Upgrade_API35" || "$sdk_level" != "35" || "$is_emulator" != "1" ]]; then
  echo "拒绝安装/强杀：仅允许 Upgrade_API35/API 35 模拟器（当前 AVD='${avd_name:-未知}' API='${sdk_level:-未知}' emulator='${is_emulator:-未知}'）。未安装或停止任何应用。" >&2
  exit 2
fi

echo "[1/4] 安装本地 debug 验证构建（不会发布）"
cd "$android_dir"
./gradlew :app:assembleDebug :app:assembleDebugAndroidTest --no-daemon
"${adb_cmd[@]}" install -r app/build/outputs/apk/debug/app-debug.apk
"${adb_cmd[@]}" install -r app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk

echo "[2/4] 运行阶段 1 测试并等待 READY：${phase1_test}（run id: ${run_id}）"
"${adb_cmd[@]}" shell am instrument -w -r \
  -e class "$test_class#$phase1_test" \
  -e forceStopHarness 1 \
  -e forceStopRunId "$run_id" \
  "$test_runner" >"$phase1_log" 2>&1 &
phase1_pid=$!
ready=0
for _ in $(seq 1 180); do
  if grep -q "forceStopState=READY" "$phase1_log"; then
    ready=1
    break
  fi
  if ! kill -0 "$phase1_pid" 2>/dev/null; then
    cat "$phase1_log" >&2
    echo "第一阶段提前结束，未到达强杀窗口。" >&2
    exit 1
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  cat "$phase1_log" >&2
  echo "等待 180 秒仍未观察到 READY；未执行强杀。" >&2
  exit 1
fi

echo "[3/4] 现在强杀应用进程，验证磁盘与断点恢复"
"${adb_cmd[@]}" shell am force-stop com.webnovel.mobile
remaining_pids="$("${adb_cmd[@]}" shell pidof com.webnovel.mobile 2>/dev/null | tr -d '\r' || true)"
if [[ -n "$remaining_pids" ]]; then
  echo "force-stop 后目标应用进程仍存活（PID: $remaining_pids）；拒绝继续并报告 PASS。" >&2
  exit 1
fi
set +e
wait "$phase1_pid"
phase1_status=$?
set -e
if [[ "$phase1_status" -eq 0 ]]; then
  echo "仪器命令正常收尾；已由 pidof 确认目标应用进程被系统停止。"
fi

echo "[4/4] 冷启动后执行阶段 2 验证：${phase2_test}"
"${adb_cmd[@]}" shell am instrument -w -r \
  -e class "$test_class#$phase2_test" \
  -e forceStopHarness 1 \
  -e forceStopRunId "$run_id" \
  "$test_runner"

echo "PASS：真实 force-stop 后数据校验和续传断言全部通过（run id: ${run_id}）"
