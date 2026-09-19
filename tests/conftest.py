# -*- coding: utf-8 -*-
"""测试环境隔离（必须在任何 app / engine 模块 import 之前生效）

解决两类真实问题：

1. 数据污染 → 假通过
   测试此前直接读写项目的 data/ 与 sources/，把 "测试源_test.example.com"
   一类的条目写进真实 data/search_cache.json。后续运行命中陈旧缓存就"通过"，
   清掉缓存就失败——测试结果取决于磁盘残留，等于没有测试。
   这里把 DATA_DIR / SOURCES_DIR 指向临时目录，并复制一份真实书源进去，
   既隔离写入，又保留 load_enabled() 有源可用的前提。

2. 真实 DNS 解析副作用
   SSRF 防护会对目标做 DNS 解析校验。测试里的 test.example.com 等假域名
   解析必然失败，且解析耗时随网络状态波动，是套件抖动的直接来源。
   测试默认关闭解析校验（WR_SSRF_SKIP_DNS=1）；需要验证解析层的用例
   自行 monkeypatch socket.getaddrinfo + urlsec._RESOLVE_DISABLED。
"""
import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="wr_test_")
_DATA = os.path.join(_TMP, "data")
_SOURCES = os.path.join(_TMP, "sources")
os.makedirs(_DATA, exist_ok=True)
os.makedirs(_SOURCES, exist_ok=True)

_HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 复制真实书源到隔离目录：测试依赖"有可用书源"，但不能写回真实目录
_real_sources = os.path.join(_HUB, "sources")
if os.path.isdir(_real_sources):
    for _f in os.listdir(_real_sources):
        if _f.endswith(".json"):
            try:
                shutil.copy2(os.path.join(_real_sources, _f),
                             os.path.join(_SOURCES, _f))
            except Exception:
                pass

os.environ["WR_DATA_DIR"] = _DATA
os.environ["WR_SOURCES_DIR"] = _SOURCES
# 避免误用真实鉴权配置
os.environ.pop("WR_AUTH_PASSWORD", None)
# 关闭 DNS 解析校验：假域名解析失败会引入耗时波动与不确定失败
os.environ["WR_SSRF_SKIP_DNS"] = "1"
# 关闭 import app 即启动的后台线程（搜索预热/源健康校验/自动追更）：
# 预热会向 127.0.0.1:8766 自调搜索接口——不关掉，跑 pytest 会真实打到
# 生产实例并改写其搜索缓存；健康校验/追更会真实访问源站。
os.environ["WR_DISABLE_BACKGROUND"] = "1"
# 双保险：即便预热线程意外启动，也指向测试专用端口而非生产 8766
os.environ["PORT"] = "18931"


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)
