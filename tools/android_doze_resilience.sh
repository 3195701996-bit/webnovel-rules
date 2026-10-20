#!/usr/bin/env bash
set -euo pipefail

# Host-controlled Doze test. Instrumentation must not be responsible for
# observing itself while Android is in forced idle: the host owns the idle
# interval and starts a fresh verifier after leaving Doze.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
android_dir="$repo_root/android"
adb_bin="${ADB:-adb}"
gradlew="${GRADLEW:-./gradlew}"
if [[ -z "${ANDROID_SERIAL:-}" ]]; then
  echo "必须显式指定 ANDROID_SERIAL=emulator-5554；拒绝在未指定设备时继续。" >&2
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
if [[ "$avd_name" != "Upgrade_API35" || "$sdk_level" != "35" || "$is_emulator" != "1" ]]; then
  echo "拒绝安装或切换 Doze：仅允许 Upgrade_API35/API 35 模拟器 " \
       "(当前 AVD='${avd_name:-未知}' API='${sdk_level:-未知}' emulator='${is_emulator:-未知}')。" >&2
  exit 2
fi

run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
idle_seconds="${DOZE_SECONDS:-40}"
if ! [[ "$idle_seconds" =~ ^[1-9][0-9]{0,2}$ ]]; then
  echo "DOZE_SECONDS 必须是 1..999 的整数。" >&2
  exit 2
fi
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/wr-doze.XXXXXX")"
phase1_log="$tmp_dir/phase1.log"
phase2_log="$tmp_dir/phase2.log"
test_runner="com.webnovel.mobile.test/androidx.test.runner.AndroidJUnitRunner"
phase2_ok=0

instrument() {
  local phase="$1" method="$2" output="$3"
  "${adb_cmd[@]}" shell am instrument -w -r \
    -e class "com.webnovel.mobile.ScreenOffDozeTest#$method" \
    -e dozeHarness 1 -e dozePhase "$phase" -e dozeRunId "$run_id" \
    "$test_runner" >"$output" 2>&1
}

cleanup() {
  local rc=$?
  # Restore only the disposable AVD state, independent of which phase failed.
  "${adb_cmd[@]}" shell dumpsys deviceidle unforce >/dev/null 2>&1 || true
  "${adb_cmd[@]}" shell input keyevent 224 >/dev/null 2>&1 || true
  "${adb_cmd[@]}" shell cmd power set-fixed-performance-mode-enabled false \
    >/dev/null 2>&1 || true
  if [[ "$rc" -ne 0 && "$phase2_ok" != 1 ]]; then
    echo "演练未完成；尝试清理本轮 __screen_off_ 测试身份。" >&2
    "${adb_cmd[@]}" shell am instrument -w -r \
      -e class "com.webnovel.mobile.ScreenOffDozeTest#cleanupOrphanedSyntheticArtifacts" \
      -e dozeHarness 1 -e dozePhase cleanup -e dozeRunId "$run_id" \
      "$test_runner" >"$tmp_dir/cleanup.log" 2>&1 || true
  fi
  if [[ "$rc" == 0 && "$phase2_ok" == 1 ]]; then
    rm -rf "$tmp_dir"
  else
    echo "Doze 演练诊断日志保留于：$tmp_dir" >&2
  fi
}
trap cleanup EXIT

echo "[1/6] 构建并安装本地 debug 验证包（不会发布）"
cd "$android_dir"
"$gradlew" :app:assembleDebug :app:assembleDebugAndroidTest --no-daemon
"${adb_cmd[@]}" install -r app/build/outputs/apk/debug/app-debug.apk
"${adb_cmd[@]}" install -r app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk

echo "[2/6] 准备随机漫画身份与真实下载页"
if ! instrument prepare prepareForExternalDoze "$phase1_log"; then
  cat "$phase1_log" >&2
  echo "阶段 1 未完成；不会进入 Doze。" >&2
  exit 1
fi
if ! rg -q "INSTRUMENTATION_STATUS: dozeReady=READY" "$phase1_log" || \
   ! rg -q "INSTRUMENTATION_STATUS: dozeRunId=$run_id" "$phase1_log"; then
  cat "$phase1_log" >&2
  echo "阶段 1 缺少本轮 READY 证据；不会进入 Doze。" >&2
  exit 1
fi
cat "$phase1_log"

echo "[3/6] 通过正式启动入口建立前台服务，再由主机强制进入 Doze"
"${adb_cmd[@]}" shell am start -W -n com.webnovel.mobile/.MainActivity
app_pid_before=""
for _ in $(seq 1 30); do
  app_pid_before="$("${adb_cmd[@]}" shell pidof com.webnovel.mobile 2>/dev/null | tr -d '\r' || true)"
  [[ -n "$app_pid_before" ]] && break
  sleep 1
done
if [[ -z "$app_pid_before" ]]; then
  echo "从正式入口启动后未观察到应用进程；不会强制进入 Doze。" >&2
  exit 1
fi
echo "正式前台服务启动后的应用 PID=$app_pid_before"

echo "[4/6] 熄屏并强制 deep Doze ${idle_seconds}s，监测目标服务进程"
"${adb_cmd[@]}" shell input keyevent 26
"${adb_cmd[@]}" shell dumpsys deviceidle force-idle
sleep 3
idle_state="$("${adb_cmd[@]}" shell dumpsys deviceidle get deep | tr -d '\r')"
idle_state_upper="$(printf '%s' "$idle_state" | tr '[:lower:]' '[:upper:]')"
echo "deep idle state=$idle_state"
if [[ "$idle_state_upper" != *IDLE* ]]; then
  echo "系统未进入 deep Doze；不把本轮计作通过。" >&2
  exit 1
fi
app_pid="$("${adb_cmd[@]}" shell pidof com.webnovel.mobile 2>/dev/null | tr -d '\r' || true)"
if [[ -z "$app_pid" ]]; then
  echo "进入 Doze 后本机前台服务进程消失；不满足后台存活要求。" >&2
  exit 1
elif [[ "$app_pid" != "$app_pid_before" ]]; then
  echo "Doze 前后应用 PID 改变（$app_pid_before → $app_pid）；进程未保持存活。" >&2
  exit 1
else
  echo "Doze 中目标应用进程保持同一 PID：$app_pid"
fi
sleep "$idle_seconds"
"${adb_cmd[@]}" shell dumpsys deviceidle unforce
"${adb_cmd[@]}" shell input keyevent 224
sleep 3

echo "[5/6] 新仪器阶段检查落盘页完整性并恢复任务"
if ! instrument verify verifyAfterExternalDozeAndResume "$phase2_log"; then
  cat "$phase2_log" >&2
  exit 1
fi
if ! rg -q "INSTRUMENTATION_STATUS: dozeRecovered=PASS" "$phase2_log" || \
   ! rg -q "INSTRUMENTATION_STATUS: dozeRunId=$run_id" "$phase2_log"; then
  cat "$phase2_log" >&2
  echo "阶段 2 缺少本轮恢复证据。" >&2
  exit 1
fi
phase2_ok=1
cat "$phase2_log"

echo "[6/6] 确认设备已离开 Doze，演练数据由用例 teardown 精确清理"
final_idle="$("${adb_cmd[@]}" shell dumpsys deviceidle get deep | tr -d '\r')"
final_idle_upper="$(printf '%s' "$final_idle" | tr '[:lower:]' '[:upper:]')"
if [[ "$final_idle_upper" == *IDLE* ]]; then
  echo "设备仍处于 deep Doze：$final_idle" >&2
  exit 1
fi
echo "PASS：外部 Doze 状态切换、下载数据校验与退出后的恢复均已验证"
