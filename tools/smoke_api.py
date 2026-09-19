#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API 冒烟清单（R47）：启动真实服务（隔离临时数据目录），逐端点断言状态码。

用途：pytest 之外的最后一道防线——模块拆分/路由变更曾导致"测试全绿但
端点 500"（如阅读进度 API 的 BOOK_PROGRESS_FILE 未定义）。此脚本起真实
HTTP 服务验证，覆盖 pytest 未直连的关键路径。

运行：venv/bin/python tools/smoke_api.py
退出码：0 = 全部通过；1 = 有失败项。
"""
import json
from pathlib import Path
import sys
import tempfile

from runtime_server import LocalServer

# (方法, 路径, 期望状态码集合, 说明)
CHECKS = [
    ("GET",  "/", {200}, "首页"),
    ("GET",  "/sources", {200}, "书源管理页"),
    ("GET",  "/library", {200}, "书库页"),
    ("GET",  "/tasks_page", {200}, "任务页"),
    ("GET",  "/manga", {200}, "漫画页"),
    ("GET",  "/reader/smoke_key", {200}, "阅读器页"),
    ("GET",  "/login", {200}, "登录页(未启用鉴权时重定向)"),
    ("GET",  "/api/health", {200}, "健康检查"),
    ("GET",  "/api/sources", {200}, "书源列表"),
    ("GET",  "/api/tasks", {200}, "任务列表"),
    ("GET",  "/api/books", {200}, "书籍列表"),
    ("GET",  "/api/net-ips", {200}, "局域网 IP"),
    ("GET",  "/api/hot-novels", {200}, "热门推荐"),
    ("GET",  "/api/manga/sources", {200}, "漫画源列表"),
    ("GET",  "/api/manga/library", {200}, "漫画书库"),
    ("GET",  "/api/manga/history", {200}, "漫画历史"),
    ("GET",  "/api/manga/favorites", {200}, "漫画收藏"),
    ("GET",  "/api/search", {400}, "搜索缺关键词 → 400"),
    # 阅读进度（R47 回归点：曾因 BOOK_PROGRESS_FILE 未定义而 500）
    ("POST", "/api/books/smoke_key/progress", {200}, "进度保存"),
    ("GET",  "/api/books/smoke_key/progress", {200}, "进度读取"),
]


def main():
    with tempfile.TemporaryDirectory(prefix="wr_smoke_") as tmp:
        root = Path(tmp)
        sources = root / "sources"
        sources.mkdir()
        with LocalServer(root / "data", sources) as server:
            fails = 0
            for method, path, expect, note in CHECKS:
                body = json.dumps({"idx": 1, "pct": 10, "name": "chapter"}).encode() \
                    if method == "POST" else None
                code, _, _ = server.request(method, path, body)
                ok = code in expect
                fails += int(not ok)
                print(f"{'PASS' if ok else 'FAIL'} {method:4s} {path:44s} {code}  {note}")
            print(f"SMOKE: {len(CHECKS) - fails}/{len(CHECKS)} passed")
            return int(bool(fails))


if __name__ == "__main__":
    sys.exit(main())
