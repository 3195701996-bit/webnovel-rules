#!/usr/bin/env bash
set -euo pipefail

# Verify an actual old-APK -> new-APK in-place upgrade on an explicitly selected
# dedicated emulator/device. This script never builds, uninstalls, or publishes.
if [[ $# -ne 3 || -z "${ANDROID_SERIAL:-}" ]]; then
  echo "用法：ANDROID_SERIAL=<ReleaseUpgrade_API35 的模拟器序列号> $0 <上一版-release.apk> <候选-release.apk> <匹配新版签名的测试APK>" >&2
  echo "环境变量：UPGRADE_OLD_VERSION_NAME/CODE（默认 1.6.4/154）、UPGRADE_NEW_VERSION_NAME（默认 2.0.0）" >&2
  echo "脚本默认只允许未安装本应用的专用 ReleaseUpgrade_API35/API 35 AVD；会在其上安装 APK。" >&2
  exit 2
fi

old_apk="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
new_apk="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
test_apk="$(cd "$(dirname "$3")" && pwd)/$(basename "$3")"
for apk in "$old_apk" "$new_apk" "$test_apk"; do
  if [[ ! -f "$apk" ]]; then
    echo "APK 不存在：$apk" >&2
    exit 2
  fi
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
android_dir="$repo_root/android"
sdk_root="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}"
if [[ -z "$sdk_root" && -n "${HOME:-}" && -d "$HOME/Library/Android/sdk" ]]; then
  sdk_root="$HOME/Library/Android/sdk"
fi
latest_build_tools=""
if [[ -n "$sdk_root" && -d "$sdk_root/build-tools" ]]; then
  latest_build_tools="$(find "$sdk_root/build-tools" -mindepth 1 -maxdepth 1 -type d \
    | sort -V | tail -n 1)"
fi
adb_bin="${ADB:-${sdk_root:+$sdk_root/platform-tools/adb}}"
apkanalyzer_bin="${APKANALYZER:-${sdk_root:+$sdk_root/cmdline-tools/latest/bin/apkanalyzer}}"
apksigner_bin="${APKSIGNER:-${latest_build_tools:+$latest_build_tools/apksigner}}"
unzip_bin="${UNZIP:-unzip}"
adb_bin="${adb_bin:-adb}"
apkanalyzer_bin="${apkanalyzer_bin:-apkanalyzer}"
apksigner_bin="${apksigner_bin:-apksigner}"
package="com.webnovel.mobile"
runner="com.webnovel.mobile.test/androidx.test.runner.AndroidJUnitRunner"
test_class="com.webnovel.mobile.UpgradeConsistencyTest"
expected_avd="${UPGRADE_AVD_NAME:-ReleaseUpgrade_API35}"
expected_old_name="${UPGRADE_OLD_VERSION_NAME:-1.6.4}"
expected_old_code="${UPGRADE_OLD_VERSION_CODE:-154}"
expected_new_name="${UPGRADE_NEW_VERSION_NAME:-2.0.0}"
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
tmp_dir=""
phase1_started=0

on_exit() {
  local rc=$?
  trap - EXIT
  if [[ $rc -ne 0 && $phase1_started -eq 1 ]]; then
    echo "升级演练失败，尝试仅清理本轮测试夹具并还原测试改动。" >&2
    set +e
    "${adb_cmd[@]}" shell am force-stop "$package" >/dev/null 2>&1
    if "${adb_cmd[@]}" shell am instrument -w -r \
      -e class "$test_class#cleanupAfterFailure" \
      -e upgradeHarness 1 \
      -e upgradeRunId "$run_id" \
      "$runner" >"$tmp_dir/cleanup.log" 2>&1 &&
      grep -Fq "UPGRADE_EVIDENCE 失败后夹具下载/小说/书目/收藏/历史及用户书源已清理或还原" "$tmp_dir/cleanup.log"; then
      echo "失败后的夹具清理与源文件恢复完成。" >&2
    else
      echo "自动清理未能确认完成；设备保持隔离，请保留日志并运行 cleanupAfterFailure 用例复核。" >&2
    fi
    set -e
  fi
  if [[ $rc -eq 0 ]]; then
    [[ -z "$tmp_dir" ]] || rm -rf "$tmp_dir"
  else
    if [[ -n "$tmp_dir" ]]; then
      echo "升级验证日志保留在：$tmp_dir" >&2
    fi
  fi
  exit "$rc"
}
trap on_exit EXIT

require_evidence() {
  local logfile="$1" marker="$2" phase="$3"
  if ! grep -Fq "$marker" "$logfile"; then
    echo "$phase 未产生明确完成证据（可能被 JUnit Assume 跳过）；拒绝报告升级 PASS" >&2
    exit 1
  fi
}

for tool in "$adb_bin" "$apkanalyzer_bin" "$apksigner_bin" "$unzip_bin"; do
  if [[ "$tool" == */* ]]; then
    [[ -x "$tool" ]] || { echo "工具不可执行：$tool" >&2; exit 2; }
  else
    command -v "$tool" >/dev/null 2>&1 || { echo "缺少工具：$tool" >&2; exit 2; }
  fi
done

adb_cmd=("$adb_bin" -s "$ANDROID_SERIAL")
if ! "${adb_cmd[@]}" get-state >/dev/null 2>&1; then
  echo "指定设备不可用：$ANDROID_SERIAL" >&2
  exit 2
fi

# This flow installs and upgrades APKs, so prove the selected target is the
# dedicated disposable AVD before doing anything beyond read-only metadata.
avd_name="$("${adb_cmd[@]}" emu avd name 2>/dev/null | sed -n '1{s/\r$//;p;}')"
sdk_level="$("${adb_cmd[@]}" shell getprop ro.build.version.sdk 2>/dev/null | tr -d '\r')"
is_emulator="$("${adb_cmd[@]}" shell getprop ro.kernel.qemu 2>/dev/null | tr -d '\r')"
if [[ "$avd_name" != "$expected_avd" || "$sdk_level" != "35" || "$is_emulator" != "1" ]]; then
  echo "拒绝安装：仅允许指定的 ${expected_avd}/API 35 模拟器（当前 AVD='${avd_name:-未知}' API='${sdk_level:-未知}' emulator='${is_emulator:-未知}'）。未安装/覆盖任何包。" >&2
  exit 2
fi

old_pkg="$("$apkanalyzer_bin" manifest application-id "$old_apk")"
new_pkg="$("$apkanalyzer_bin" manifest application-id "$new_apk")"
test_pkg="$("$apkanalyzer_bin" manifest application-id "$test_apk")"
old_code="$("$apkanalyzer_bin" manifest version-code "$old_apk")"
new_code="$("$apkanalyzer_bin" manifest version-code "$new_apk")"
old_name="$("$apkanalyzer_bin" manifest version-name "$old_apk")"
new_name="$("$apkanalyzer_bin" manifest version-name "$new_apk")"
old_cert="$("$apksigner_bin" verify --print-certs "$old_apk" | sed -n 's/^Signer #1 certificate SHA-256 digest: //p' | head -n 1)"
new_cert="$("$apksigner_bin" verify --print-certs "$new_apk" | sed -n 's/^Signer #1 certificate SHA-256 digest: //p' | head -n 1)"
test_cert="$("$apksigner_bin" verify --print-certs "$test_apk" | sed -n 's/^Signer #1 certificate SHA-256 digest: //p' | head -n 1)"

[[ "$old_pkg" == "$package" && "$new_pkg" == "$package" ]] || {
  echo "包名不匹配：旧=$old_pkg 新=$new_pkg 预期=$package" >&2; exit 2;
}
[[ "$old_code" == "$expected_old_code" && "$old_name" == "$expected_old_name" ]] || {
  echo "上一版基线必须是 ${expected_old_name}/code ${expected_old_code}：实际 $old_name/code $old_code" >&2; exit 2;
}
[[ "$new_name" == "$expected_new_name" ]] || {
  echo "候选新版必须是 ${expected_new_name}：实际 $new_name" >&2; exit 2;
}
[[ "$test_pkg" == "$package.test" ]] || {
  echo "测试 APK 包名不匹配：$test_pkg 预期=$package.test" >&2; exit 2;
}
[[ "$old_code" =~ ^[0-9]+$ && "$new_code" =~ ^[0-9]+$ && "$old_code" -lt "$new_code" ]] || {
  echo "versionCode 必须递增：旧=$old_code 新=$new_code" >&2; exit 2;
}
[[ -n "$old_cert" && "$old_cert" == "$new_cert" && "$new_cert" == "$test_cert" ]] || {
  echo "旧版、新版和测试 APK 必须使用同一签名证书。" >&2; exit 2;
}

# The instrumentation APK must be built from precisely the same clean source
# and version as the release candidate; an old test APK can otherwise pass
# while never exercising the candidate's migration code.
identity_entry="assets/build-identity.properties"
if ! new_identity="$($unzip_bin -p "$new_apk" "$identity_entry" 2>/dev/null)" ||
   ! test_identity="$($unzip_bin -p "$test_apk" "$identity_entry" 2>/dev/null)"; then
  echo "候选 APK 与 instrumentation APK 都必须包含构建身份元数据。" >&2; exit 2
fi
identity_value() {
  local identity="$1" key="$2"
  printf '%s\n' "$identity" | sed -n "s/^${key}=//p" | head -n 1
}
new_revision="$(identity_value "$new_identity" revision)"
new_dirty="$(identity_value "$new_identity" dirty)"
new_identity_name="$(identity_value "$new_identity" versionName)"
new_identity_code="$(identity_value "$new_identity" versionCode)"
test_revision="$(identity_value "$test_identity" revision)"
test_dirty="$(identity_value "$test_identity" dirty)"
test_identity_name="$(identity_value "$test_identity" versionName)"
test_identity_code="$(identity_value "$test_identity" versionCode)"
[[ "$new_revision" =~ ^[0-9a-fA-F]{40}$ && "$new_dirty" == "clean" ]] || {
  echo "新版 APK 必须来自完整 Git SHA 且源码状态为 clean。" >&2; exit 2;
}
[[ "$new_identity_name" == "$new_name" && "$new_identity_code" == "$new_code" ]] || {
  echo "新版 APK 构建身份与 manifest 版本不一致。" >&2; exit 2;
}
[[ "$test_revision" == "$new_revision" && "$test_dirty" == "clean" &&
   "$test_identity_name" == "$new_name" && "$test_identity_code" == "$new_code" ]] || {
  echo "instrumentation APK 必须与新版 APK 使用同一 clean revision 和版本。" >&2; exit 2;
}

if ! package_listing="$("${adb_cmd[@]}" shell pm list packages "$package" 2>&1)"; then
  echo "拒绝安装：无法核实 ${package} 的安装状态：${package_listing}。未安装/覆盖任何包。" >&2
  exit 2
fi
package_listing="$(printf '%s\n' "$package_listing" | tr -d '\r')"
if [[ -n "$package_listing" ]] && ! grep -Eq '^package:[A-Za-z0-9_.]+$' <<<"$package_listing"; then
  echo "拒绝安装：无法判定目标应用安装状态：${package_listing}。未安装/覆盖任何包。" >&2
  exit 2
fi
if grep -Fxq "package:${package}" <<<"$package_listing"; then
  echo "目标设备已安装 ${package}；演练只允许使用未安装过本应用的专用空白 AVD，避免覆盖或清理任何已有用户数据。未安装/覆盖任何包。" >&2
  exit 2
fi

# Create diagnostic storage only after every read-only precondition passes.
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/wr-upgrade.XXXXXX")"
trap on_exit EXIT

echo "测试 APK：${test_apk}（已验证构建 SHA/版本与候选一致且 clean）"
echo "安全条件：目标为 ${expected_avd}/API 35 空白模拟器，且未安装过 ${package}；演练生成的漫画/收藏/历史均为本轮夹具。"
echo "目标设备：${ANDROID_SERIAL}（${avd_name}/API ${sdk_level}）；包：${package}；版本：${old_name}/${old_code} -> ${new_name}/${new_code}"
echo "[1/5] 安装旧版 APK（保留该专用设备上此应用的现有数据）"
"${adb_cmd[@]}" install -r "$old_apk"
"${adb_cmd[@]}" shell am force-stop "$package"
"${adb_cmd[@]}" shell monkey -p "$package" 1 >/dev/null
sleep 5
installed_old="$("${adb_cmd[@]}" shell dumpsys package "$package" | sed -n 's/.*versionCode=\([0-9]*\).*/\1/p' | head -n 1 | tr -d '\r')"
[[ "$installed_old" == "$old_code" ]] || { echo "旧版启动后版本不符：$installed_old" >&2; exit 1; }

echo "[2/5] 在旧版本运行期间建立书架、阅读进度、部分下载与用户源修改证据"
"${adb_cmd[@]}" install -r "$test_apk"
phase1_started=1
"${adb_cmd[@]}" shell am instrument -w -r \
  -e class "$test_class#phase1_setupState" \
  -e upgradeHarness 1 \
  -e upgradeRunId "$run_id" \
  "$runner" | tee "$tmp_dir/phase1.log"
require_evidence "$tmp_dir/phase1.log" "UPGRADE_EVIDENCE 阶段 1 完成" "阶段 1"

echo "[3/5] 以 install -r 覆盖安装新版，保留应用私有数据"
"${adb_cmd[@]}" install -r "$new_apk"
installed_new="$("${adb_cmd[@]}" shell dumpsys package "$package" | sed -n 's/.*versionCode=\([0-9]*\).*/\1/p' | head -n 1 | tr -d '\r')"
[[ "$installed_new" == "$new_code" ]] || { echo "覆盖升级后版本不符：$installed_new" >&2; exit 1; }

echo "[4/5] 新版首次启动后验证用户数据、下载字节、任务状态、进度和源文件"
"${adb_cmd[@]}" shell am force-stop "$package"
"${adb_cmd[@]}" shell monkey -p "$package" 1 >/dev/null
sleep 5
"${adb_cmd[@]}" shell am instrument -w -r \
  -e class "$test_class#phase2_verifyAfterUpgrade" \
  -e upgradeHarness 1 \
  -e upgradeRunId "$run_id" \
  "$runner" | tee "$tmp_dir/phase2.log"
require_evidence "$tmp_dir/phase2.log" "UPGRADE_EVIDENCE 阶段 2 通过并清理完成" "阶段 2"

echo "[5/5] PASS：真实旧版运行 -> 同签名新版覆盖安装 -> 新版数据保全断言通过。"
echo "注意：设备已更新到 ${new_code}；请使用专用验证设备。run id=${run_id}"
