# -*- coding: utf-8 -*-
"""真实网络探针的**隔离保护**（指南 P1-2）。

为什么有它：这些工具会访问真实源站，适配器/代理池可能产生本地状态；
而历史上有工具（`verify_sources_live.py`）会**直接写 `<repo>/sources/*.json` 去"禁用无效源"**
—— 那是**用户运行数据**，不该被一次随手运行改掉（指南 §5：书源/代理/缓存/健康数据
不得混进修复、不得被无关操作覆盖）。

用法（放在任何网络探针的最前面、导入 engine 之前）：

    from _probe_guard import ensure_isolated
    ensure_isolated()                       # 数据目录自动隔离到临时目录
    ensure_isolated(allow_source_writes=True)   # 确实要改书源时才显式打开

生效方式：
  · `WR_DATA_DIR` 未设 → 自动指向临时目录（真实 `data/` 不会被读写）；
  · 默认设 `WR_SOURCES_READONLY=1` → `engine.source_mgr._atomic_write` 拒绝写书源目录；
    确需写入时显式 `WR_ALLOW_SOURCE_WRITES=1`（或传 allow_source_writes=True）。
"""
import os
import tempfile


def ensure_isolated(profile="mobile", allow_source_writes=False):
    data_dir = os.environ.get("WR_DATA_DIR")
    if not data_dir:
        data_dir = tempfile.mkdtemp(prefix="wr-probe-")
        os.environ["WR_DATA_DIR"] = data_dir
        print(f"[probe-guard] 数据目录已自动隔离：{data_dir}（真实 data/ 不会被读写）",
              flush=True)
    os.environ.setdefault("WR_PROFILE", profile)
    os.environ.setdefault("WR_TEST", "1")
    os.environ.setdefault("WR_DISABLE_BACKGROUND", "1")
    if allow_source_writes or os.environ.get("WR_ALLOW_SOURCE_WRITES") == "1":
        os.environ.pop("WR_SOURCES_READONLY", None)
        print("[probe-guard] 注意：书源目录**允许写入**（显式打开）", flush=True)
    else:
        os.environ["WR_SOURCES_READONLY"] = "1"
        print("[probe-guard] 书源目录按只读处理（确需写入请设 WR_ALLOW_SOURCE_WRITES=1）",
              flush=True)
    return data_dir
