#!/bin/bash
# webnovel_rules 启动/重启脚本
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
# 清理 pyc
find "$DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
# 杀旧进程
PID=$(lsof -ti :8766 2>/dev/null)
[ -n "$PID" ] && kill -9 $PID && sleep 2
# 启动
nohup ./venv/bin/python -B app.py --port 8766 >> data/logs/server.log 2>&1 &
sleep 4
curl -s -o /dev/null -w "服务器启动: HTTP %{http_code}\n" --max-time 8 http://127.0.0.1:8766/api/health || echo "启动失败"
