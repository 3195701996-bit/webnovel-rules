#!/usr/bin/env bash
# Verify a candidate APK against the last shipped APK before any distribution.
# This script is read-only: it never builds, installs, uploads, or publishes.
set -euo pipefail

usage() {
  echo "用法: $0 <上一版-release.apk> <候选-release.apk> <期望版本名> <期望版本码>" >&2
  echo "环境变量: ANDROID_BUILD_TOOLS=/path/to/android-sdk/build-tools/<version>" >&2
}

if [[ $# -ne 4 ]]; then usage; exit 2; fi
previous_apk="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
candidate_apk="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
expected_name="$3"
expected_code="$4"
expected_revision="${EXPECTED_GIT_REVISION:-}"
if [[ ! -f "$previous_apk" || ! -f "$candidate_apk" ]]; then
  echo "上一版或候选 APK 文件不存在" >&2; exit 2
fi
if [[ ! "$expected_code" =~ ^[1-9][0-9]*$ ]]; then
  echo "期望版本码必须是正整数" >&2; exit 2
fi
if [[ -z "$expected_name" ]]; then
  echo "期望版本名不能为空" >&2; exit 2
fi
if [[ ! "$expected_revision" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "必须通过 EXPECTED_GIT_REVISION 指定本次发布的完整 40 位 Git SHA" >&2; exit 2
fi

build_tools="${ANDROID_BUILD_TOOLS:-}"
if [[ -z "$build_tools" && -n "${ANDROID_HOME:-}" ]]; then
  build_tools="$(find "$ANDROID_HOME/build-tools" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort -V | tail -n 1)"
fi
aapt_bin="${AAPT:-${build_tools:+$build_tools/aapt}}"
apksigner_bin="${APKSIGNER:-${build_tools:+$build_tools/apksigner}}"
if [[ -z "$aapt_bin" ]]; then aapt_bin="$(command -v aapt || true)"; fi
if [[ -z "$apksigner_bin" ]]; then apksigner_bin="$(command -v apksigner || true)"; fi
if [[ -z "$aapt_bin" || ! -x "$aapt_bin" || -z "$apksigner_bin" || ! -x "$apksigner_bin" ]]; then
  echo "需要 Android SDK build-tools 中的 aapt 与 apksigner" >&2; exit 2
fi

metadata() {
  local apk="$1" badging
  badging="$("$aapt_bin" dump badging "$apk")"
  local line
  line="$(printf '%s\n' "$badging" | sed -n '1p')"
  local package version_code version_name
  package="$(printf '%s\n' "$line" | sed -n "s/.*package: name='\([^']*\)'.*/\1/p")"
  version_code="$(printf '%s\n' "$line" | sed -n "s/.*versionCode='\([^']*\)'.*/\1/p")"
  version_name="$(printf '%s\n' "$line" | sed -n "s/.*versionName='\([^']*\)'.*/\1/p")"
  if [[ -z "$package" || -z "$version_code" || -z "$version_name" ]]; then
    echo "无法解析 APK manifest: $apk" >&2; return 1
  fi
  printf '%s\t%s\t%s\n' "$package" "$version_code" "$version_name"
}

signer_sha256() {
  local apk="$1" output digest
  output="$(LC_ALL=C "$apksigner_bin" verify --verbose --print-certs "$apk")"
  printf '%s\n' "$output" | grep -Eq 'Verified using v2 scheme \(APK Signature Scheme v2\): true|Verified using v3 scheme \(APK Signature Scheme v3\): true' || {
    echo "APK 缺少有效的 v2/v3 签名: $apk" >&2; return 1;
  }
  digest="$(printf '%s\n' "$output" | sed -n 's/^Signer #1 certificate SHA-256 digest: //p' | head -n 1 | tr '[:upper:]' '[:lower:]')"
  if [[ ! "$digest" =~ ^[0-9a-f]{64}$ ]]; then
    echo "无法读取 APK 签名证书 SHA-256: $apk" >&2; return 1
  fi
  printf '%s\n' "$digest"
}

IFS=$'\t' read -r previous_package previous_code previous_name < <(metadata "$previous_apk")
IFS=$'\t' read -r candidate_package candidate_code candidate_name < <(metadata "$candidate_apk")
if [[ "$previous_package" != "com.webnovel.mobile" || "$candidate_package" != "$previous_package" ]]; then
  echo "应用 ID 不一致或不是 com.webnovel.mobile" >&2; exit 1
fi
if [[ "$candidate_name" != "$expected_name" || "$candidate_code" != "$expected_code" ]]; then
  echo "候选包版本不符：实际 ${candidate_name}/${candidate_code}，期望 ${expected_name}/${expected_code}" >&2; exit 1
fi
if (( candidate_code <= previous_code )); then
  echo "候选版本码必须高于上一版：上一版 ${previous_code}，候选 ${candidate_code}" >&2; exit 1
fi

previous_signer="$(signer_sha256 "$previous_apk")"
candidate_signer="$(signer_sha256 "$candidate_apk")"
if [[ "$candidate_signer" != "$previous_signer" ]]; then
  echo "候选包签名证书与上一版不一致，覆盖升级会失败" >&2; exit 1
fi
unzip -tqq "$candidate_apk" || { echo "候选 APK ZIP 结构校验失败" >&2; exit 1; }
identity_entry="assets/build-identity.properties"
if ! unzip -p "$candidate_apk" "$identity_entry" >/dev/null 2>&1; then
  echo "候选 APK 缺少构建身份元数据: $identity_entry" >&2; exit 1
fi
identity="$(unzip -p "$candidate_apk" "$identity_entry")"
identity_value() {
  local key="$1"
  printf '%s\n' "$identity" | sed -n "s/^${key}=//p" | head -n 1
}
candidate_revision="$(identity_value revision)"
candidate_dirty="$(identity_value dirty)"
identity_name="$(identity_value versionName)"
identity_code="$(identity_value versionCode)"
if [[ ! "$candidate_revision" =~ ^[0-9a-fA-F]{40}$ || "$candidate_dirty" != "clean" ]]; then
  echo "候选 APK 必须来自完整 Git SHA 且打包源码状态为 clean" >&2; exit 1
fi
if [[ "$identity_name" != "$candidate_name" || "$identity_code" != "$candidate_code" ]]; then
  echo "候选 APK 构建身份与 manifest 版本不一致" >&2; exit 1
fi
candidate_revision_normalized="$(printf '%s' "$candidate_revision" | tr '[:upper:]' '[:lower:]')"
expected_revision_normalized="$(printf '%s' "$expected_revision" | tr '[:upper:]' '[:lower:]')"
if [[ "$candidate_revision_normalized" != "$expected_revision_normalized" ]]; then
  echo "候选 APK 源码 revision 与指定 revision 不一致" >&2; exit 1
fi
if command -v shasum >/dev/null 2>&1; then
  checksum="$(shasum -a 256 "$candidate_apk" | awk '{print $1}')"
else
  checksum="$(sha256sum "$candidate_apk" | awk '{print $1}')"
fi

printf 'APK release 验收通过\n应用: %s\n上一版: %s / code %s\n候选版: %s / code %s\n源码 revision: %s (clean)\n证书 SHA-256: %s\nAPK SHA-256: %s\n' \
  "$candidate_package" "$previous_name" "$previous_code" "$candidate_name" "$candidate_code" "$candidate_revision" "$candidate_signer" "$checksum"
