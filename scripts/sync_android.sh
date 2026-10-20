#!/usr/bin/env bash
# Compatibility entry: the current Gradle build owns the Python/Web snapshot.
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  echo "用法: scripts/sync_android.sh [--check <APK> | --help]"
  echo "无参数: 调用 android/gradlew :app:syncPythonSnapshot，不写 android_app/。"
  echo "--check <APK>: 只读校验 APK 内 Web 资源，不代表全部 Python/运行时验收。"
}

if [[ $# -eq 0 ]]; then
  exec "$repo_root/android/gradlew" -p "$repo_root/android" :app:syncPythonSnapshot --no-daemon
fi
case "$1" in
  --help|-h)
    usage
    ;;
  --check)
    if [[ $# -ne 2 ]]; then usage >&2; exit 2; fi
    python_bin="${PYTHON:-$repo_root/venv/bin/python}"
    if [[ ! -x "$python_bin" ]]; then python_bin=python3; fi
    exec "$python_bin" "$repo_root/tools/check_android_web_assets.py" "$2"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
