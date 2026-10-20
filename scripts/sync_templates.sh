#!/usr/bin/env bash
# 已废弃的模板同步名称；仅转发到当前 Gradle 快照/只读 APK 资源检查。
#
# 用法：
#   scripts/sync_templates.sh          同步（= sync_android.sh）
#   scripts/sync_templates.sh --check <APK>  只读检查内置 Web 资源
set -euo pipefail

cd "$(dirname "$0")/.."
echo "注意：旧模板同步入口已转到 Gradle 快照任务。"
exec bash scripts/sync_android.sh "$@"
