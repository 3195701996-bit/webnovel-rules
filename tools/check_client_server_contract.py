"""客户端解析器 ↔ 服务端响应的**契约核对**（自动抽键 + 真实响应比对）。

为什么需要它：客户端 `EngineData.kt` 里的解析函数按字段名读 JSON，而字段名由服务端
决定。任何一侧改名，客户端就静默拿到默认值（0/""/false）——**不报错、只是数字变成 0 或
文案变空**，属于最难发现的一类缺陷。这类漂移已经手工抓到 4 起：

  · `POST /progress` 写的是 `idx`，离线索引按自己文档读 `index` → 永远读到 0；
  · 流式搜索缓存路径把 `timed_out_sources` 塞进 `done` → 界面显示"已返回 0/N 个源"；
  · 检查更新的缺失数：服务端一直发 `missing_count`，客户端用 new+failed 自己算；
  · 图片扩展名/缓存键算法两端不一致。

做法：
  1. 用正则从 `android/.../EngineData.kt` 抽出每个 `fun xxx(body: String)` 里读的键
     （`optString("k")` / `optInt("k")` / `optBoolean` / `optLong` / `optJSONArray` …）；
  2. 按下面的 ENDPOINTS 表调用**真实接口**（数据目录隔离、内容自造）；
  3. 对每个键在响应里做存在性检查（含嵌套路径，如 `progress.total`）。

用法：
  PYTHONDONTWRITEBYTECODE=1 ./venv/bin/python tools/check_client_server_contract.py
退出码非 0 表示发现漂移。
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_DATA = os.path.join(ROOT, "android", "app", "src", "main", "java",
                           "com", "webnovel", "mobile", "EngineData.kt")

# 每个解析函数 → 真实接口 + 该响应里"键所在层级"（嵌套用点号）
#   fn: ("GET", "/api/books", "")  → 顶层对象（或数组元素，见 ROW_ARRAY）
# 说明：只有能自造夹具的接口才纳入；需要联网的（搜索/详情）不在其中。
ENDPOINTS = {
    "novels":        ("GET", "/api/books", "books"),
    "bookDetail":    ("GET", "/api/books/{book}", ""),
    "tasks":         ("GET", "/api/tasks", "tasks"),
    "storageUsage":  ("GET", "/api/storage", ""),
    "backupScope":   ("GET", "/api/backup/scope", ""),
    "manga":         ("GET", "/api/manga/library", "comics"),
    "sources":       ("GET", "/api/manga/sources", "sources"),
    "bookSources":   ("GET", "/api/sources", "sources"),
    "mangaHistory":  ("GET", "/api/manga/history", ""),
    "readProgress":  ("GET", "/api/books/{book}/progress", ""),
    "chapter":       ("GET", "/api/books/{book}/chapter/1", ""),
    "verifications": ("GET", "/api/sources/verify/results", "items"),
    "verifySummary": ("GET", "/api/sources/verify/results", ""),
    "verifyMeta":    ("GET", "/api/sources/verify/results", ""),
    "mangaLibraryUpdate": ("GET", "/api/manga/library/check-updates/status", ""),
    # 下面两个走"打桩源站层、服务端真实组装"（见 stub_sources()）：
    # 只替换**最外层的源调用**，分组/字段/统计仍由服务端代码产生——否则就是拿我造的
    # 载荷去核对客户端，等于自己跟自己对齐。
    "novelSearch":   ("GET", "/api/search?q=%E5%89%91%E6%9D%A5", "groups"),
    "mangaSearch":   ("GET", "/api/manga/search?q=%E5%89%91%E6%9D%A5", "results"),
    # 探索：源清单完全本地（读书源配置）；书单要打桩 engine.explore.explore
    "exploreSources":      ("GET", "/api/explore/sources", ""),
    "exploreMangaNote":    ("GET", "/api/explore/sources", ""),
    "exploreMangaSources": ("GET", "/api/explore/sources", ""),
    "exploreBooks":        ("GET", "/api/explore?source={uid}&url=%2Flist&page=1", ""),
    # 章节**重爬**的响应（客户端用它判断"这次真的恢复了没有"）
    "recoveredContent":    ("POST", "/api/books/{book}/chapter/1", ""),
}

# 解析函数**故意不纳入**核对时必须在这里写清原因（含糊不许进表）——
# 否则接口改名/新增解析函数时，"没被覆盖"会以"没人发现"的方式悄悄发生。
# 由**别的通道**核对的解析函数（不是"没覆盖"）：必须在工具里找得到对应的核对段落
COVERED_ELSEWHERE = {
    "engineSummary": "走「原生状态通道核对」一节（/__mobile/status 是移动入口的 WSGI 包装层，"
                     "不经 Flask 路由，test_client 到不了），那一节按它的需求逐条溯源",
}

SKIPPED_PARSERS = {
    "mangaDetail": "需要漫画适配器在线（详情来自源站）；真机/联网核对，离线工具不假装覆盖",
    "mangaChapterPages": "同上（章节图片清单来自源站）",
    "mangaChapterUrls": "同上（/urls 批量接口的每页加载方式来自源站与本地磁盘实扫）",
    "mangaUpdateCheck": "同上（漫画检查更新要打源站）",
    "novelUpdateCheck": "读的是 /check-update 轮询响应，已有 7 项 JVM 单测钉住文案与缺失数口径",
}

# 只在**特定状态**才出现的键：夹具没构造那个状态时会"缺"，属正常。
# 每一条都必须写明"何时才出现"，含糊不清就不许进这张表（否则工具会变成橡皮图章）。
CONDITIONAL = {
    "/api/tasks": {
        "tasks[].progress.done": "漫画任务的 progress 用 done；小说任务用 completed"
                                 "（客户端按 type 分支读，done 只是回退键）",
        "tasks[].manga_key": "只有漫画任务有（小说任务没有 manga_key，客户端仅用于删除漫画任务）",
        "tasks[].type": "漫画任务发 type=manga；小说任务现在也发 type=novel（本夹具含小说任务）",
        "tasks[].progress.speed": "只有**漫画**任务的 progress 带速度（下载管理器写的）；"
                                  "小说任务由爬虫写，没有 speed",
        "tasks[].progress.eta": "同上（漫画下载才估剩余时间）",
    },
    "/api/backup/scope": {
        "include[].what": "include/exclude 数组元素键；由 inc/exclude 变量间接读出，工具按元素层核对",
    },
    "/api/manga/library": {
        "comics[].dl_done": "仅当该书有**正在运行**的下载任务时才带",
        "comics[].dl_total": "同上",
        "comics[].read_note": "仅当续读只能**近似定位**（目录变过）时才带",
        "comics[].source_name": "书库列表**不展示** sourceName（书架无消费方）；详情页的 source_name "
                                "由详情接口提供（/api/manga/<source>/<comic_id> 一直有）",
    },
    "/api/books/{book}/chapter/1": {
        "reason": "仅当本章**没有缓存**（downloaded=false）时才带，说明为什么没有正文",
    },
    "/api/manga/sources": {
        "sources[].failed_stage": "仅当最近一次实测**失败**时才有失败阶段",
    },
    "/api/manga/search": {
        "results[].id": "仅当 results[] 元素**没有** sources[] 数组时走旧形态兼容分支（客户端源码里那一段），"
                        "新形态的条目在 results[].sources[] 里",
        "results[].source": "同上（旧形态兼容分支）",
        "results[].source_name": "同上（旧形态兼容分支）",
    },
    "/api/sources": {
        "sources[].valid": "仅当用户点过「校验」按钮（/api/sources/<uid>/validate 写回源文件）才有",
        "sources[].valid_error": "同上",
        "sources[].valid_tested_at": "同上",
    },
}

OPT_TYPES = "JSONObject|JSONArray|String|Int|Long|Boolean|Double"
# 循环/下标变量：`arr.optString(it)` 是取**元素的值**，不是读键名
LOOP_VARS = ("i", "it", "j", "idx", "index")
# 一个 `optXxx("键")` 或 `optXxx("键", 默认值)` 调用。
# **必须支持第二个参数**：`p.optInt("completed", p.optInt("done"))` 这种带默认值的
# 读法第一版整条匹配不上 → 那个键**从来没被核对过**（改名都不会有人发现，
# 是"客户端读 completed / 引擎改名 completedX"的变异验证抓出来的）。
_ARG = r'(?:"([^"]*)"|([A-Za-z_][A-Za-z0-9_]*))'
_DEFAULT = r'(?:\s*,\s*[^()]*(?:\([^()]*\)[^()]*)*)?'
# 一条"链"：base 变量 + 若干段 .optXxx("k") / .optXxx("k", 默认值)
CHAIN = re.compile(
    r'([A-Za-z_][A-Za-z0-9_]*)\s*\??\.\s*'
    r'((?:opt(?:' + OPT_TYPES + r')\s*\(\s*' + _ARG + _DEFAULT + r'\s*\)\s*\??\.?\s*)+)')
SEG = re.compile(r'opt(' + OPT_TYPES + r')\s*\(\s*' + _ARG + _DEFAULT + r'\s*\)')
ROOT_VAR = re.compile(r'val\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:runCatching\s*\{[^}]*\})?'
                      r'[^\n]*JSONObject\s*\(\s*body\s*\)')


def parse_requirements(fn_name):
    """抽出该解析函数**按层级**需要的键。

    返回 {"top": [...], "array": {名称: [键…]}, "object": {名称: [键…]}}
    其中数组的键可以写成 `checkpoint.done` 表示"元素里的嵌套对象 checkpoint 的 done"。

    实现是**链式解析**（不是把所有 opt*("k") 一锅端）：先把变量按来源分层
    （顶层 / 某数组的元素 / 某嵌套对象 / 元素里的嵌套对象），再沿
    `base.optJSONObject("a")?.optInt("b")` 这样的链逐段推进层级。
    第一版"一锅端"的做法会把包装键、元素键、嵌套键混在一起，拿去比对全是假阳性
    ——工具一旦噪报就没人信，等于没有。
    """
    with open(ENGINE_DATA, encoding="utf-8") as _f:
        src = _f.read()
    m = re.search(r'\n    fun ' + re.escape(fn_name) + r'\(', src)
    if not m:
        return None
    i = m.end()
    j = src.find("\n    fun ", i)
    body = src[i:j if j > 0 else len(src)]

    # ── 第一遍：变量 → 层级 ──
    # level 取值：("top","") / ("array", 名称) / ("array", 名称, 嵌套对象名)
    #             / ("object", 名称)
    #
    # 顺序有讲究：**先定对象、再按对象修正数组的父级路径**。
    # 反过来的话，处理 `val arr = o.optJSONArray("sources")` 时还不认识 o 是
    # `optJSONObject("manga")`，数组就被记成顶层 `sources` —— 核对时会去顶层找它，
    # 找不到就当成"本夹具未构造该状态"跳过（看起来是跳过，其实是漏检）。
    level = {}
    for vm in ROOT_VAR.finditer(body):
        level[vm.group(1)] = ("top", "")

    def _pass_levels():
        """一轮层级推导。**要迭代到不动点**，因为这些规则互相依赖：

        `val arr = …optJSONArray("tasks")`  →  `val o = arr.optJSONObject(i)`
        →  `val p = o.optJSONObject("progress")`

        元素变量规则要等数组规则先认出 arr，嵌套对象规则又要等元素规则先认出 o。
        单趟扫描（第一版）必然在中间断链：`p` 落成顶层对象 `progress`，
        核对时报"响应里根本没有这个对象"——而它其实在 `tasks[]` 里面（假缺失）。
        """
        # ① 嵌套对象变量：val X = Y.optJSONObject("k")，**继承父级层级**
        _strict_obj = set()
        for vm in re.finditer(r'val\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'
                              r'([A-Za-z_][A-Za-z0-9_]*)\s*(?:!!|\?)?\s*\.\s*'
                              r'optJSONObject\s*\(\s*"([^"]+)"', body):
            base = level.get(vm.group(2))
            if base and base[0] == "array":
                level[vm.group(1)] = ("array", base[1], vm.group(3))
            elif base and base[0] == "object":
                level[vm.group(1)] = ("object", f"{base[1]}.{vm.group(3)}")
            else:
                level[vm.group(1)] = ("object", vm.group(3))
            _strict_obj.add(vm.group(1))
        # 兜底（`val sm = runCatching { JSONObject(body).optJSONObject("summary") }…`）：
        # 必须能**覆盖** ROOT_VAR 的判定，否则 12 个键会被记到顶层（假缺失）；
        # 但已被严格规则处理过的变量不许被降级（那会丢掉数组上下文）。
        for vm in re.finditer(r'val\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[^\n]*?'
                              r'optJSONObject\s*\(\s*"([^"]+)"', body):
            if vm.group(1) not in _strict_obj:
                level[vm.group(1)] = ("object", vm.group(2))
        # ② 数组变量：挂在嵌套对象下的要带上父级路径
        _strict_arr = set()
        for vm in re.finditer(r'val\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'
                              r'([A-Za-z_][A-Za-z0-9_]*)\s*(?:!!|\?)?\s*\.\s*'
                              r'optJSONArray\s*\(\s*"([^"]+)"', body):
            base = level.get(vm.group(2))
            if base and base[0] == "object":
                level[vm.group(1)] = ("array", f"{base[1]}.{vm.group(3)}")
            else:
                level[vm.group(1)] = ("array", vm.group(3))
            _strict_arr.add(vm.group(1))
        for vm in re.finditer(r'val\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[^\n]*?'
                              r'optJSONArray\s*\(\s*"([^"]+)"', body):
            if vm.group(1) not in _strict_arr:
                level[vm.group(1)] = ("array", vm.group(2))
        # ③ 数组元素变量：val X = arr!!.optJSONObject(i|it|index)
        #    （Kotlin 的 `!!` 也要认，否则元素里的键一个都收不到）
        for vm in re.finditer(r'val\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'
                              r'([A-Za-z_][A-Za-z0-9_]*)\s*(?:!!|\?)?\s*\.\s*'
                              r'optJSONObject\s*\(\s*(?:i|it|idx|index)\s*\)', body):
            base = level.get(vm.group(2))
            if base and base[0] == "array":
                level[vm.group(1)] = ("array", base[1], base[2] if len(base) > 2 else "")

    _seen_sig = None
    for _ in range(6):                    # 迭代到不动点（层级链最长也就三四层）
        _pass_levels()
        sig = tuple(sorted((k, tuple(v)) for k, v in level.items()))
        if sig == _seen_sig:
            break
        _seen_sig = sig

    out = {"top": set(), "array": {}, "object": {}, "unresolved": set()}

    def add_array(arr, key):
        out["array"].setdefault(arr, set()).add(key)

    def add_object(obj, key):
        out["object"].setdefault(obj, set()).add(key)

    def walk(base_lv, tail):
        """沿链推进；tail 是若干 optXxx 段（SEG 匹配解析）"""
        lv = base_lv
        for sm in SEG.finditer(tail):
            typ, lit, var = sm.group(1), sm.group(2), sm.group(3)
            key = lit if lit is not None else var
            if typ == "JSONObject":
                if lit is None and key in LOOP_VARS:
                    # 取元素（`arr.optJSONObject(i)`）：层级不变
                    continue
                if lit is None and key not in LOOP_VARS:
                    # 变量当**键名**用（`o.optJSONObject(k)`）：这里不做数据流，
                    # 如实登记为"未解析"，由 main() 判失败 —— 宁可逼着写清楚，
                    # 也不许把 `k` 当成一个真的键名去比对（那是噪声）。
                    out["unresolved"].add(f"optJSONObject({key})")
                    continue
                if lv[0] == "array":
                    lv = ("array", lv[1], key)
                elif lv[0] == "object":
                    lv = ("object", f"{lv[1]}.{key}")
                else:
                    lv = ("object", key)
            elif typ == "JSONArray":
                # 嵌套数组要带上父级路径：`o.optJSONObject("manga")?.optJSONArray("sources")`
                # 是 `manga.sources`。第一版把它记成顶层 `sources`，于是核对时会去顶层
                # 找一个不存在的数组 → 报"该夹具未构造该状态"（看起来像跳过，其实漏检）。
                if lv[0] == "object":
                    lv = ("array", f"{lv[1]}.{key}")
                elif lv[0] == "array" and len(lv) > 2:
                    lv = ("array", f"{lv[1]}.{lv[2]}.{key}")
                else:
                    lv = ("array", key)
            else:
                if lit is None and key in LOOP_VARS:
                    # `arr.optString(it)`：这是取**元素本身的值**（字符串数组/数字数组），
                    # 不是读某个键。第一版把 `it` 当成键名，给这些接口加核对时会报出
                    # 根本不存在的"缺失键 it"（噪声），正是"工具一旦噪报就没人信"。
                    continue
                if lit is None:
                    out["unresolved"].add(f"opt{typ}({key})")
                    continue
                if lv[0] == "array":
                    # 元素内的**嵌套对象**：前缀是该对象名（`checkpoint.done`）。
                    # 前缀为空时不能拼出个点来（`len(lv) > 2` 但 lv[2] == "" 时
                    # 旧写法会生成 ".group" 这种键，核对时永远对不上 → 假缺失）。
                    prefix = f"{lv[2]}." if len(lv) > 2 and lv[2] else ""
                    add_array(lv[1], f"{prefix}{key}")
                elif lv[0] == "object":
                    add_object(lv[1], key)
                else:
                    out["top"].add(key)
        return lv

    for cm in CHAIN.finditer(body):
        base = level.get(cm.group(1))
        if base is None:
            continue
        walk(base, cm.group(2))
    return {k: (sorted(v) if isinstance(v, set) else {kk: sorted(vv) for kk, vv in v.items()})
            for k, v in out.items()}


def _real_uid():
    """取一个**真实存在的**源 uid（夹具里凡是会被服务端拿去 get_by_uid 的字段都要用它）。

    踩过的坑：夹具 `_state.json` 里写 `source_uid: "src"`，章节重爬
    `POST /api/books/<k>/chapter/<n>` 用 get_by_uid 找源 → 404 →
    "响应里没有 ok/content" 被当成客户端契约漂移。**那是夹具问题，不是漂移**。
    """
    try:
        from engine.source_mgr import load_all
        for s in (load_all() or []):
            if s.get("uid"):
                return s["uid"]
    except Exception:                                            # noqa: BLE001
        pass
    return "src"


def _prepare(root):
    """造夹具：一本有缓存的书 + 一个漫画下载目录（写进服务端真正读的目录）"""
    import server.novel_api as na
    data = os.path.dirname(os.path.abspath(na.BOOKS_DIR.rstrip("/")))
    os.makedirs(os.path.join(data, "books", "src_deadbeef"), exist_ok=True)
    from engine.app_utils import cache_key_of
    chs = [{"name": f"第{i}章", "url": f"https://t.test/read/1/{i}.html"} for i in range(1, 4)]
    d = os.path.join(data, "books", "src_deadbeef")
    for c in chs[:2]:
        with open(os.path.join(d, cache_key_of(c["url"]) + ".cache"), "w",
                  encoding="utf-8") as f:
            f.write(c["name"] + "\n\n正文。")
    with open(os.path.join(d, "_state.json"), "w", encoding="utf-8") as f:
        json.dump({"book": {"name": "测试书", "author": "作者",
                            "source_uid": _real_uid(),
                            "book_url": "https://t.test/read/1/"},
                   "chapters": chs, "completed": [c["url"] for c in chs[:2]],
                   "failed": {chs[2]["url"]: "源站 403"}}, f, ensure_ascii=False)
    # 漫画下载目录 + 书库
    md = os.path.join(data, "manga", "downloads", "copymanga", "comic_x", "ch1")
    os.makedirs(md, exist_ok=True)
    for i in range(1, 4):
        with open(os.path.join(md, f"{i:04d}.jpg"), "w") as f:
            f.write("x")
    os.makedirs(os.path.join(data, "manga"), exist_ok=True)
    with open(os.path.join(data, "manga", "_library.json"), "w", encoding="utf-8") as f:
        # downloaded_at：真实写入方（engine/manga/download_manager.py）每条都带，
        # 客户端书架"最近下载"排序读它；夹具与真实记录保持一致
        json.dump([{"source": "copymanga", "comic_id": "comic_x", "title": "测试漫画",
                    "images": 3, "chapters": 1, "status": "done",
                    "downloaded_at": "2026-01-01 00:00:00"}], f, ensure_ascii=False)
    return data


def stub_sources():
    """把**源站调用层**换成固定数据，让服务端走真实的组装/分组/统计代码。

    · 小说：`engine.crawler.SourceCrawler.search` → 固定书目（含分组需要的
      作者/简介/封面/字数/最新章节等）；
    · 漫画：`server.manga_api._manga_search_one` → 固定条目（含 id/title/author/
      cover/tags/source/source_name）；
    · 探索：`engine.explore.explore` → 固定榜单书单（分类地址本来就要抓源站）。
    替换的是"网络"这一层，不是响应本身。
    """
    from engine.crawler import SourceCrawler
    import server.manga_api as ma
    from engine import explore as ex

    def fake_explore(src, url, page=1, timeout=15):
        return [{"name": f"榜单书{page}", "author": "榜单作者", "intro": "简介",
                 "cover": "https://img.test/e.jpg", "book_url": f"https://s.test/e/{page}",
                 "last_chapter": "第10章", "source_uid": (src or {}).get("uid") or "stub",
                 "source_name": (src or {}).get("bookSourceName") or "stub"}]

    def fake_novel_search(self, keyword, page=1):
        uid = (self.source or {}).get("uid") or "stub_uid"
        return [{"name": keyword, "author": "测试作者", "intro": "测试简介",
                 "cover": "https://img.test/c.jpg", "book_url": "https://s.test/read/1/",
                 "last_chapter": "第1300章", "chapter_count": 1300,
                 "word_count": "500万字", "update_time": "2026-09-01",
                 "source_uid": uid, "source_name": (self.source or {}).get("bookSourceName") or uid}]

    def fake_manga_search(a, q, page, order):
        return a.key, [{"id": "comic_1", "title": q, "author": "漫画作者",
                        "cover": "https://img.test/m.jpg", "tags": ["动作", "冒险"],
                        "source": a.key, "source_name": a.name, "total": 1}], ""

    def fake_get_content(self, url, timeout=25, **kw):
        return "重爬回来的正文。" * 3

    SourceCrawler.search = fake_novel_search
    SourceCrawler.get_content = fake_get_content
    ma._manga_search_one = fake_manga_search
    ex.explore = fake_explore


def _json_parsers():
    """EngineData.kt 里**真正在读 JSON** 的函数名集合。

    判据：有 `body: String` 形参，且函数体里出现 JSON 读取（JSONObject(...)/optXxx）。
    只按"有没有 body 形参"判断是不够的——`mangaPos(label, page)` 这类格式化函数
    也会被算进来，于是覆盖面统计会虚高。
    """
    with open(ENGINE_DATA, encoding="utf-8") as f:
        src = f.read()
    out = set()
    for m in re.finditer(r'\n    fun ([A-Za-z_][A-Za-z0-9_]*)\(([^)]*)\)', src):
        name, params = m.group(1), m.group(2)
        if "String" not in params:
            continue
        nxt = src.find("\n    fun ", m.end())
        body = src[m.end():nxt if nxt > 0 else len(src)]
        if re.search(r'JSONObject\s*\(|opt(?:JSONObject|JSONArray|String|Int|Long|'
                     r'Boolean|Double)\s*\(', body):
            out.add(name)
    return out


def _dig(payload, path):
    """按点号路径取值（如 `manga.sources`）——核对时必须用**客户端真正读的那条路径**，
    否则嵌套数组会被当成"顶层没这个数组"而静默跳过。"""
    return _dig2(payload, path)[0]


def _dig2(payload, path):
    """返回 `(值, 路径是否真的不存在)`。

    **JSON null 算"存在"**：服务端明确发了 `"progress": null`（文件还没生成），
    客户端也有兜底（`?: JSONObject()`）—— 这跟"根本没有这个键/路径"是两件事。
    分不开的话，前者会被报成"改名了"（假缺失），后者会被当成"夹具没构造"（漏检）。
    """
    cur = payload
    for seg in path.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return None, True
        cur = cur[seg]
    return cur, False


def main():
    tmp = tempfile.mkdtemp(prefix="wr-contract-")
    os.environ.update(WR_PROFILE="mobile", WR_TEST="1", WR_DISABLE_BACKGROUND="1",
                      WR_DATA_DIR=os.path.join(tmp, "runtime"),
                      WR_SOURCES_DIR=os.path.join(ROOT, "sources"))
    sys.path.insert(0, ROOT)
    _prepare(tmp)
    stub_sources()
    import app as appmod
    c = appmod.app.test_client()
    # 写一条阅读进度：/progress 只有存在记录时才返回 idx/pct/name
    c.post("/api/books/src_deadbeef/progress",
           json={"idx": 2, "pct": 30, "name": "第2章"})

    # 造一条**能用真实源**的小说任务：源 uid 不存在时任务会立刻失败，
    # progress 里就没有 current/total —— 那样核对出来的"缺失"是夹具问题，不是漂移。
    from engine.source_mgr import load_enabled
    _srcs = load_enabled() or []
    if _srcs:
        c.post("/api/tasks", json={"source_uid": _srcs[0].get("uid"),
                                   "book_url": "https://t.test/read/1/"})
        time.sleep(4)   # 等它把 progress 写出来（含 total/current）

    problems, checked, skipped = [], 0, 0
    # 探索书单要一个**真实存在且声明了榜单**的源 uid（接口自己会校验 uid 是否存在）
    _ex_uid = ""
    try:
        _exs = (c.get("/api/explore/sources").get_json() or {}).get("sources") or []
        _ex_uid = (_exs[0] or {}).get("uid") or ""
    except Exception:                                            # noqa: BLE001
        _ex_uid = ""
    for fn, (method, path, where) in ENDPOINTS.items():
        req = parse_requirements(fn)
        if req is None:
            problems.append(f"⚠ 找不到解析函数 {fn}（EngineData.kt 改名了？）")
            continue
        if "{uid}" in path and not _ex_uid:
            skipped += 1
            print(f"  ◇ {fn:24s} {path:28s} 没有声明榜单的源，跳过")
            continue
        p = path.replace("{book}", "src_deadbeef").replace("{uid}", _ex_uid)
        r = c.get(p) if method == "GET" else c.post(p, json={})
        try:
            payload = r.get_json()
        except Exception:                                        # noqa: BLE001
            payload = None
        if payload is None:
            problems.append(f"⚠ {fn}: {method} {p} 未返回 JSON（HTTP {r.status_code}）")
            continue
        miss = []
        # 查表按**不带查询串**的路径：登记表里写 `/api/manga/search`，
        # 而不是把 `?q=…` 也抄进去（抄了就会随搜索词变化而失配）。
        # 注意：下面判断"数组/对象路径不存在"时就要用它，所以必须在这里先算。
        cond = CONDITIONAL.get(path.split("?")[0], {})
        # 顶层键
        for k in req["top"]:
            if isinstance(payload, dict) and k not in payload:
                miss.append(k)
        # 数组元素的键（数组名可以是点号路径，如 `manga.sources`）
        for arr_name, ks in req["array"].items():
            node, absent = _dig2(payload, arr_name)
            if absent:
                # **路径不存在** ≠ "夹具没构造这个状态"：客户端读的数组名/路径被改名时，
                # 出现的正是"路径不存在"。第一版把两者一起当"跳过"，于是漏检伪装成跳过
                # —— 是变异验证（把 `optJSONArray("sources")` 改成 `"sourcesX"`）抓出来的。
                if f"{arr_name}[]" in cond:
                    skipped += 1
                    print(f"  ◇ {fn:24s} {path:28s} {arr_name}[] 条件数组未出现"
                          f"（已登记：{cond[f'{arr_name}[]'][:24]}）")
                else:
                    miss.append(f"{arr_name}[]（响应里根本没有这个数组/路径）")
                continue
            if node is None:
                # 服务端明确发了 null（如 progress 文件还没生成）：客户端有兜底，
                # 这不是"改名"，当成条件状态如实说明即可
                skipped += 1
                print(f"  ◇ {fn:24s} {path:28s} {arr_name}[] 值为 null（服务端明确给了空值）")
                continue
            if not isinstance(node, list):
                miss.append(f"{arr_name}[]（不是数组）")
                continue
            if not node:
                skipped += 1
                print(f"  ◇ {fn:24s} {path:28s} {arr_name}[] 空数组（本夹具未构造该状态）")
                continue
            first = node[0]
            if not isinstance(first, dict):
                miss.append(f"{arr_name}[]（元素不是对象）")
                continue
            for k in ks:
                if "." in k:
                    head, _, tail = k.partition(".")
                    sub = first.get(head)
                    if not isinstance(sub, dict) or tail not in sub:
                        miss.append(f"{arr_name}[].{k}")
                elif k not in first:
                    miss.append(f"{arr_name}[].{k}")
        # 嵌套对象的键（对象名同样可以是点号路径）
        for obj_name, ks in req["object"].items():
            sub, absent = _dig2(payload, obj_name)
            if absent:
                # 同上：对象缺失多数是"夹具没构造"，但**改名**也是这个表现，
                # 所以必须登记过才允许跳过。
                if obj_name in cond:
                    skipped += 1
                    print(f"  ◇ {fn:24s} {path:28s} 嵌套对象 {obj_name} 未出现"
                          f"（已登记：{cond[obj_name][:24]}）")
                else:
                    miss.append(f"{obj_name}（响应里根本没有这个对象/路径）")
                continue
            if sub is None:
                # 同上：明确 null（如 `"progress": null`）不是改名
                skipped += 1
                print(f"  ◇ {fn:24s} {path:28s} 嵌套对象 {obj_name} 值为 null"
                      f"（服务端明确给了空值，客户端有兜底）")
                continue
            if not isinstance(sub, dict):
                miss.append(f"{obj_name}（不是对象）")
                continue
            miss += [f"{obj_name}.{k}" for k in ks if k not in sub]
        # 变量当键名：不做数据流，如实报出来（否则工具会以"没这回事"的方式漏检）
        for u in sorted(req.get("unresolved") or []):
            problems.append(f"⚠ {fn}: {path} 有变量当键名读法 {u}："
                            f"无法静态确定客户端读的是哪个键")
        cond_hit = [m for m in miss if m in cond]
        miss = [m for m in miss if m not in cond]
        checked += 1
        if cond_hit:
            print(f"  ◇ {fn:24s} {path:28s} 条件键未覆盖 {len(cond_hit)} 个"
                  f"（例：{cond_hit[0]} → {cond.get(cond_hit[0], '')[:28]}）")
        if miss:
            problems.append(f"⚠ {fn}: {path} 缺少客户端会读的键 → {miss}")
            print(f"  ✗ {fn:24s} {path:28s} 缺 {miss}")
        else:
            n = len(req["top"]) + sum(len(v) for v in req["array"].values()) \
                + sum(len(v) for v in req["object"].values())
            print(f"  ✓ {fn:24s} {path:28s} {n} 个键（分层）全部存在")

    # ── 第二阶段：**数值级**核对（键存在不代表算得对）──────────────
    print("-" * 78)
    print("数值核对：")
    st = c.get("/api/storage").get_json() or {}
    cats = st.get("categories") or []
    cat_sum = sum(int(x.get("bytes") or 0) for x in cats)
    if int(st.get("total_bytes") or 0) != cat_sum:
        problems.append(f"⚠ /api/storage: total_bytes={st.get('total_bytes')} "
                        f"≠ 分类之和 {cat_sum}")
        print(f"  ✗ total_bytes 与分类之和不一致：{st.get('total_bytes')} vs {cat_sum}")
    else:
        print(f"  ✓ /api/storage total_bytes = 分类之和 = {cat_sum}")
    # 夹具里的小说占用可精确算出：服务端的口径是**整个书目录**（`_dir_bytes`，
    # 含 _state.json / 缓存 / book.txt 等，语义是"这本书在本机占多少"）。
    # 第一版只算了 .cache 就对不上——那是**核对口径写窄了**，不是服务端算错。
    import server.novel_api as na
    bk = os.path.join(na.BOOKS_DIR, "src_deadbeef")
    real = 0
    for _root, _dirs, _files in os.walk(bk):
        for _fn in _files:
            real += os.path.getsize(os.path.join(_root, _fn))
    row = next((x for x in (st.get("novel_books") or [])
                if x.get("key") == "src_deadbeef"), None)
    if row is None:
        problems.append("⚠ /api/storage: 夹具书未出现在 novel_books 里")
        print("  ✗ 夹具书未出现在 novel_books")
    elif int(row.get("bytes") or 0) != real:
        problems.append(f"⚠ /api/storage: 该书 bytes={row.get('bytes')} ≠ 磁盘实际 {real}")
        print(f"  ✗ 小说缓存字节对不上：接口 {row.get('bytes')} vs 磁盘 {real}")
    else:
        print(f"  ✓ 小说缓存字节与磁盘一致（{real} 字节，{row.get('cached')} 章）")
    # 备份范围：纳入/排除之和 vs 总量
    bs = c.get("/api/backup/scope").get_json() or {}
    inc_b = int((bs.get("include_detail") or {}).get("bytes") or 0)
    exc_b = int(bs.get("excluded_total_bytes") or 0)
    print(f"  ✓ 备份范围：纳入 {inc_b} 字节 · 排除 {exc_b} 字节"
          f"（两者都是独立统计，不做相等断言）")

    # 原生状态通道（/__mobile/status）：由移动入口的 WSGI 包装层生成，
    # 桌面上不经 HTTP，但仍要核对"客户端读的键，生成侧确实有"。
    print("原生状态通道核对（/__mobile/status）：")
    try:
        # 由**客户端的需求**驱动（而不是我手写两个键名）：
        #   engineSummary 读 runtime.state / runtime.wsgi / capabilities.missing
        _req = parse_requirements("engineSummary") or {"top": [], "array": {}, "object": {}}
        with open(os.path.join(ROOT, "server", "mobile_entry.py"),
                  encoding="utf-8") as _f:
            _src = _f.read()
        _i = _src.index("def status(self):")
        _body = _src[_i:_i + 900]
        # 生产者溯源：每条需求必须能在生成侧指出**它从哪里来**
        _trace = {
            "capabilities": '{"ok": True, "runtime": self.status(), "capabilities": …} 包了一层',
            "runtime": '同上：runtime 就是 self.status() 的返回',
            "capabilities.missing": 'capabilities.summary() 的 missing（下面直接调用该方法核对）',
            "runtime.state": 'self.status() 的返回 dict 里的 "state"',
            "runtime.wsgi": 'self.status() 的返回 dict 里的 "wsgi"',
        }
        for name, ks in (_req["object"] or {}).items():
            for k in ks:
                full = f"{name}.{k}"
                if full not in _trace:
                    problems.append(f"⚠ /__mobile/status: 客户端读了 {full}，"
                                    f"但生成侧溯源表里没有它（新增字段要补溯源）")
                    print(f"  ✗ 无溯源：{full}")
                    continue
                leaf = k
                if f'"{leaf}"' not in _body and not (name == "capabilities" and leaf == "missing"):
                    problems.append(f"⚠ /__mobile/status: 生成侧没有 {full}")
                    print(f"  ✗ 生成侧缺 {full}")
                else:
                    print(f"  ✓ {full}（{_trace[full]}）")
        for k in (_req["top"] or []):
            if f'"{k}"' not in _src:
                problems.append(f"⚠ /__mobile/status: 生成侧没有顶层键 {k}")
                print(f"  ✗ 顶层缺 {k}")
            else:
                print(f"  ✓ {k}")
        from server import capabilities as _cap
        _sm = _cap.summary()
        if "missing" not in (_req["object"].get("capabilities") or []) and \
                "missing" not in _sm:
            problems.append("⚠ /__mobile/status: capabilities 缺少 missing")
        else:
            print(f"  ✓ capabilities.missing 实调 summary()："
                  f"{len(_sm.get('missing') or [])} 项")
    except Exception as e:                                       # noqa: BLE001
        problems.append(f"⚠ 原生状态通道核对失败：{type(e).__name__}: {e}")

    # 覆盖面不变式：每个"读 JSON 的解析函数"要么被核对，要么在 SKIPPED_PARSERS 写明原因。
    # 没有这条，接口改名/新增解析函数时"没被覆盖"会以"没人发现"的方式发生。
    try:
        _parsers = _json_parsers()
        _uncovered = sorted(f for f in _parsers
                            if f not in ENDPOINTS and f not in SKIPPED_PARSERS
                            and f not in COVERED_ELSEWHERE)
        if _uncovered:
            problems.append(f"⚠ 这些解析函数在读 JSON，但既没核对也没登记原因：{_uncovered}")
        _stale = sorted(f for f in SKIPPED_PARSERS if f not in _parsers)
        if _stale:
            problems.append(f"⚠ SKIPPED_PARSERS 登记了不存在（或不是 JSON 解析函数）的名字："
                            f"{_stale}（登记表过期了？）")
        print(f"覆盖面：JSON 解析函数 {len(_parsers)} 个 · 走接口核对 "
              f"{len(_parsers & set(ENDPOINTS))} 个 · 其它通道 "
              f"{len(_parsers & set(COVERED_ELSEWHERE))} 个 · 登记跳过 "
              f"{len(_parsers & set(SKIPPED_PARSERS))} 个")
    except Exception as e:                                       # noqa: BLE001
        problems.append(f"⚠ 覆盖面核对失败：{type(e).__name__}: {e}")

    print(f"核对接口 {checked} 个 · 空列表/缺层级跳过 {skipped} 处 · 发现问题 {len(problems)} 个")
    for p in problems:
        print("  " + p)
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
