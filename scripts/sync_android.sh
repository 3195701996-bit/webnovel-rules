#!/usr/bin/env bash
# 将桌面权威源同步到 Android 内嵌 Python 目录（单一事实源机制）。
#
# 背景：Chaquopy 只认 android_app/app/src/main/python/ 下的代码，历史上这里
# 是一份手工维护的镜像（桌面 app.py + server/ 合并旧快照 + engine/ 副本），
# 两端独立编辑导致严重分叉。现拍板：桌面代码为唯一权威源，安卓镜像由本脚本
# 生成，禁止手改镜像。
#
# 同步内容（桌面 → 镜像）：
#   engine/ server/ templates/ static/ sources/ 整目录（含 --delete 清孤儿）
#   app.py 单文件
#
# 保护清单（镜像侧独有，绝不删除/覆盖）：
#   wr_mobile.py   —— 安卓启动入口（设置 WR_PLATFORM=android 后 import app）
#   curl_cffi/     —— 请求 shim（安卓 pip 无 curl_cffi，转发到 requests）
#   data/          —— 安卓私有数据占位目录
# 保护机制：上述文件均位于镜像根，而同步仅覆盖清单内的子目录/文件，
#   根级其他内容天然不受影响。
#
# 用法：
#   scripts/sync_android.sh          同步（幂等，可反复执行）
#   scripts/sync_android.sh --check  仅检查（CI 用，不一致时退出码 1）
set -euo pipefail

cd "$(dirname "$0")/.."
DST="android_app/app/src/main/python"

[ -d "$DST" ] || { echo "找不到 $DST"; exit 1; }

# 同步项：目录（递归 + --delete）与单文件
SYNC_DIRS=(engine server templates static sources)
SYNC_FILES=(app.py)
# 不计入一致性判断的噪音文件
EXCLUDE_RE='__pycache__|\.DS_Store'

_check_one() {
    # $1=源路径；输出差异行（已过滤噪音），无差异则无输出
    local src="$1" dst="$DST/$1"
    if [ ! -e "$dst" ]; then
        echo "镜像缺失: $dst"
        return
    fi
    diff -rq "$src" "$dst" 2>&1 | grep -vE "$EXCLUDE_RE" || true
}

if [ "${1:-}" = "--check" ]; then
    out=""
    for d in "${SYNC_DIRS[@]}" "${SYNC_FILES[@]}"; do
        [ -e "$d" ] || { echo "找不到权威源 $d"; exit 1; }
        out+="$(_check_one "$d")"
    done
    if [ -z "$out" ]; then
        echo "安卓镜像与桌面权威源一致"
        exit 0
    fi
    echo "安卓镜像与桌面权威源不一致："
    echo "$out"
    echo
    echo "运行 scripts/sync_android.sh 同步"
    exit 1
fi

n=0
for d in "${SYNC_DIRS[@]}"; do
    [ -d "$d" ] || { echo "找不到权威源 $d"; exit 1; }
    mkdir -p "$DST/$d"
    # -a 递归保留属性；--delete 清掉镜像中权威源已不存在的孤儿文件；
    # 排除 __pycache__/.DS_Store（构建缓存，不入 APK 源树）
    rsync -a --delete \
        --exclude '__pycache__' --exclude '*.pyc' --exclude '.DS_Store' \
        "$d/" "$DST/$d/"
    echo "  同步 $d/"
    n=$((n + 1))
done
for f in "${SYNC_FILES[@]}"; do
    [ -f "$f" ] || { echo "找不到权威源 $f"; exit 1; }
    cp "$f" "$DST/$f"
    echo "  同步 $f"
    n=$((n + 1))
done

echo "已同步 $n 项（幂等：重复执行结果相同）"
# 清理镜像中的解释器缓存（compileall/本地模拟启动会产生，勿入 APK 源树；
# 仅删缓存目录与字节码，不动任何 .py 源文件）
find "$DST" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "$DST" -name '*.pyc' -delete 2>/dev/null || true
echo "保护项未动：wr_mobile.py、curl_cffi/、data/"
echo "注意：android_app/app/build/ 下的副本是 Gradle 构建产物，会自动重建，勿手改"
