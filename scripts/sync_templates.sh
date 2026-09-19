#!/usr/bin/env bash
# 已废弃：本脚本曾只同步 templates/ 到 Android 内嵌副本。
# 现安卓镜像整体由 scripts/sync_android.sh 维护（engine/server/app.py/
# templates/static/sources 全量，单一事实源）。本脚本保留为薄封装，
# 兼容旧调用方（含 CI），实际工作全部委托给 sync_android.sh。
#
# 用法：
#   scripts/sync_templates.sh          同步（= sync_android.sh）
#   scripts/sync_templates.sh --check  仅检查（= sync_android.sh --check）
set -euo pipefail

cd "$(dirname "$0")/.."
echo "注意：sync_templates.sh 已并入 sync_android.sh（全量同步），此处转发调用"
exec bash scripts/sync_android.sh "${1:-}"
