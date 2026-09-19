# -*- coding: utf-8 -*-
"""逐源功能级验证（诊断 §6）：搜索 → 详情 → 目录 → 正文，逐阶段记录**真实**结果。

判定规则（不把"依赖装上了"或"搜索能返回"当作"这本书能读"）：

  verified     搜索、目录、正文三个阶段都拿到真实内容（且带证据数字）
  partial      搜索通过，但目录或正文阶段失败（记录失败阶段与原因）
  failed       搜索阶段就失败
  unsupported  规则需要 JS 执行（当前引擎不执行 JS），
               或该源用到的规则类型缺少对应依赖（cssselect / lxml / jsonpath_ng）
  skipped      书源已停用，本轮未纳入

结果原子写入 data/source_verified.json，含时间、关键词、每阶段耗时与证据，
供界面如实展示"通过/部分通过/失败/未支持/未验证"，未验证的绝不标成通过。
"""
import json
import os
import threading
import time

from engine import neterr as _ne
from engine.app_utils import StageTimeout, call_bounded, pick_title_match
from engine.config import DATA_DIR

VERIFIED_FILE = os.path.join(DATA_DIR, "source_verified.json")

# 判定顺序固定，避免不同机器上结果不一致
STAGES = ("search", "toc", "content")

# 每个阶段的预算（秒）：手机上比桌面更保守
DEFAULT_BUDGET = {
    "search": 15,
    "book": 15,
    "toc": 30,
    "content": 30,
}

# 正文长度分两档（中文小说一章通常数千字）：
#   < MIN_CONTENT_CHARS（200）   判 partial：太短，不足以说明"这本书能读"
#   < SUSPECT_CONTENT_CHARS（1000）仍判 verified，但带 note 提示"建议抽查"——
# 既不高估（300 字也当通过），也不误伤（有些源首章很短或是公告页）
MIN_CONTENT_CHARS = 200
SUSPECT_CONTENT_CHARS = 1000

# 单次运行最多验证多少个源（超出需显式分批）
MAX_LIMIT = 34
DEFAULT_LIMIT = 6

_JS_MARKERS = ("@js:", "<js>", "</js>", "java.", "@JS:", "webView", "@webjs")

_lock = threading.Lock()
_job = {"status": "idle", "done": 0, "total": 0, "current": "", "keyword": "",
        "started_at": "", "finished_at": "", "error": ""}
_results = None


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _iter_rule_strings(obj):
    """递归取出所有规则字符串（书源里规则值可以是字符串/列表/字典）。"""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for x in obj:
            yield from _iter_rule_strings(x)
    elif isinstance(obj, dict):
        for x in obj.values():
            yield from _iter_rule_strings(x)


def needs_js(src):
    """规则里是否含 JS 标记 → 当前引擎不执行 JS，必须如实标为 unsupported。"""
    for s in _iter_rule_strings(src):
        low = s.lower()
        for m in _JS_MARKERS:
            if m.lower() in low:
                return True, f"规则含 JS（{m}），当前引擎不执行 JS 脚本"
    return False, ""


def required_deps(src):
    """按**实际用到的规则类型**推断依赖（不是猜整体）。

    - CSS 选择器（legado 默认写法，如 ".book@li@a"）：cssselect
    - XPath（"//" 开头或 @XPath:）：lxml
    - JSONPath（"$" 开头或 @json:）：jsonpath_ng
    """
    deps = set()
    keys = ("ruleSearch", "ruleBookInfo", "ruleToc", "ruleContent")
    for k in keys:
        for s in _iter_rule_strings(src.get(k) or {}):
            t = s.strip()
            if not t:
                continue
            low = t.lower()
            if low.startswith("@css:") or "@css:" in low:
                deps.add("cssselect")
            if t.startswith("//") or low.startswith("@xpath:"):
                deps.add("lxml")
            if t.startswith("$") or low.startswith("@json:") or "$." in t:
                deps.add("jsonpath_ng")
            if "@@" in t or "@" in t:
                # legado 的 "." + "@" 链式写法走 CSS 解析
                deps.add("cssselect")
    return deps


def _missing_deps(deps):
    from server import capabilities as caps
    probe = caps.probe()
    return sorted(d for d in deps if not probe.get(d, {}).get("available"))


def classify(stages, unsupported_reason=""):
    """把阶段结果归类成状态。顺序：unsupported → verified → partial → failed。"""
    if unsupported_reason:
        return "unsupported", unsupported_reason
    s = stages.get("search") or {}
    if not s.get("ok"):
        return "failed", s.get("detail") or "搜索失败"
    t = stages.get("toc") or {}
    c = stages.get("content") or {}
    if t.get("ok") and c.get("ok"):
        chars = c.get("chars") or 0
        if chars < MIN_CONTENT_CHARS:
            return "partial", (f"正文偏短（{chars} 字 < {MIN_CONTENT_CHARS}），"
                               f"可能只取到片段，不能算通过")
        return "verified", ""
    if not t.get("ok"):
        return "partial", "目录阶段失败：" + (t.get("detail") or "无章节")
    return "partial", "正文阶段失败：" + (c.get("detail") or "无正文")


def verify_one(src, keyword=None, budget=None, now=_now):
    """验证单个书源，返回结构化结果（含每阶段证据）。异常一律转成阶段失败，不抛出。"""
    b = dict(DEFAULT_BUDGET)
    b.update(budget or {})
    uid = src.get("uid") or ""
    name = src.get("bookSourceName") or uid
    out = {"uid": uid, "name": name, "status": "failed", "reason": "",
           "tested_at": now(), "stages": {}, "keyword": keyword or ""}

    js, js_reason = needs_js(src)
    if js:
        out["status"], out["reason"] = "unsupported", js_reason
        return out
    deps = required_deps(src)
    miss = _missing_deps(deps)
    if miss:
        out["status"] = "unsupported"
        out["reason"] = "缺少规则所需依赖：" + "、".join(miss) + "（能力台账已标注）"
        out["needs"] = sorted(deps)
        return out

    from engine.crawler import SourceCrawler
    kw = keyword or (src.get("ruleSearch") or {}).get("checkKeyWord") or ""
    if not kw:
        kw = "剑来"      # 与 validate_source 的兜底一致

    keyword_used = kw
    try:
        crawler = SourceCrawler(src)
    except Exception as e:
        out["stages"]["search"] = {"ok": False, "detail": f"爬虫初始化异常：{type(e).__name__}"}
        out["status"], out["reason"] = "failed", out["stages"]["search"]["detail"]
        return out

    # ── 阶段 1：搜索 ──
    t0 = time.monotonic()
    books = []
    err = ""
    # 换关键词只在"没搜到"时才有意义；连接类/超时类失败换词只是把同一段超时等 4 遍
    # （漫画侧同款缺陷实测：单源 120 秒）。这里给每阶段套**硬性时限**。
    _hard_fail = False
    for kw_try in [kw] + [k for k in ("剑来", "诡秘之主", "凡人修仙传") if k != kw]:
        try:
            books = call_bounded(lambda kk=kw_try: crawler.search(kk, page=1),
                                 b["search"]) or []
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:100]}"
            books = []
            if isinstance(e, StageTimeout) or _ne.classify(e):
                _hard_fail = True
        if books:
            keyword_used = kw_try
            break
        if _hard_fail:
            break
        err = err or "搜索无结果"
    out["keyword"] = keyword_used
    ms = int((time.monotonic() - t0) * 1000)
    if not books:
        out["stages"]["search"] = {"ok": False, "ms": ms, "detail": err or "搜索无结果"}
        out["status"], out["reason"] = "failed", out["stages"]["search"]["detail"]
        return out
    b0 = pick_title_match(books, keyword_used)
    out["stages"]["search"] = {
        "ok": True, "ms": ms, "count": len(books),
        "detail": f"搜索到 {len(books)} 条",
        "sample": {"name": b0.get("name", ""), "book_url": b0.get("book_url", "")},
    }
    book_url = b0.get("book_url") or ""

    # ── 阶段 2：详情 + 目录 ──
    t1 = time.monotonic()
    chapters = []
    toc_err = ""
    toc_attempts = 0

    def _fetch_toc():
        book = crawler.get_book(book_url, fast=True, deadline=b["book"]) if book_url else None
        if book is None:
            return [], "详情为空"
        # 目录不设人为小上限（0.74.17 修误判）：旧实现写 max_pages=3，
        # 规则引擎源的分页目录会被截成 3 页 → 章数偏低甚至撞上限报错，
        # 而适配器源本来就忽略该参数（实测精华书阁 1309 章照常）。
        # 真正的边界交给 deadline（b["toc"]*2），不靠拍一个页数。
        chs = crawler.get_toc(book, timeout=b["toc"],
                              deadline=b["toc"] * 2) or []
        return chs, ("" if chs else "目录为空")

    # 移动网络下取目录经常只是**一次抖动**（同一 URL 用 curl 实测 200/115KB）。
    # 只重试一次：既能把抖动和真实故障区分开，也不会把真失败重试成"看起来通过"。
    for attempt in range(2):
        toc_attempts = attempt + 1
        try:
            chapters, toc_err = _fetch_toc()
        except Exception as e:
            chapters, toc_err = [], f"{type(e).__name__}: {str(e)[:100]}"
        if chapters:
            break
    ms = int((time.monotonic() - t1) * 1000)
    if not chapters:
        out["stages"]["toc"] = {"ok": False, "ms": ms, "detail": toc_err or "目录为空",
                                "attempts": toc_attempts}
        out["status"], out["reason"] = classify(out["stages"])
        return out
    c0 = chapters[0] if isinstance(chapters[0], dict) else {}
    out["stages"]["toc"] = {
        "ok": True, "ms": ms, "count": len(chapters), "attempts": toc_attempts,
        "detail": f"目录 {len(chapters)} 章",
        "sample": {"name": c0.get("name", ""), "url": c0.get("url", "")},
    }

    # ── 阶段 3：正文 ──
    t2 = time.monotonic()
    text = ""
    c_err = ""
    try:
        # 正文**必须走完整分页**（0.74.17 修误判）：旧实现写 max_pages=1，
        # 而 R78 起"撞上限仍有下一页 = 整章失败（不静默截断）" —— 于是任何
        # **章节跨页**的源在逐源验证里必然失败，并被标成 android_partial，
        # 用户书源页会看到"正文阶段失败：分页数达到上限 1 页"这种吓人理由。
        # 实测（2026-09-18）：顶点(m版) 被如此误判，而它正文其实正常
        # （同章另测 2982 字）。边界同样交给 deadline。
        text = crawler.get_content(c0.get("url", ""), timeout=b["content"],
                                   deadline=b["content"] * 2) or ""
        if not text.strip():
            c_err = "正文为空"
    except Exception as e:
        c_err = f"{type(e).__name__}: {str(e)[:100]}"
    ms = int((time.monotonic() - t2) * 1000)
    if not text.strip():
        out["stages"]["content"] = {"ok": False, "ms": ms, "detail": c_err or "正文为空"}
    else:
        n = len(text.strip())
        # 净化质量自检（0.74.6）：把"正文干不干净"也变成**可查的事实**。
        # 只记录、不改判定——广告残留不等于验证失败（三个阶段确实都拿到真内容了），
        # 但用户与排查者都该能看到；判据与探针共用 engine.cleaner.check_purify_quality。
        try:
            from engine.cleaner import check_purify_quality
            _q_ok, _q_probs = check_purify_quality(text.strip())
        except Exception:                                        # noqa: BLE001
            _q_ok, _q_probs = True, []
        out["stages"]["content"] = {"ok": True, "ms": ms, "chars": n,
                                    "quality_ok": bool(_q_ok),
                                    "quality_issues": _q_probs,
                                    "detail": f"正文 {n} 字"
                                              + ("" if _q_ok else " · " + "；".join(_q_probs[:2]))}
        if not _q_ok:
            out["note"] = ((out.get("note") or "") +
                           ("；" if out.get("note") else "") +
                           "正文质量待抽查：" + "；".join(_q_probs[:2]))
        if MIN_CONTENT_CHARS <= n < SUSPECT_CONTENT_CHARS:
            out["note"] = f"正文偏短（{n} 字），建议抽查是否只取到片段"
    out["status"], out["reason"] = classify(out["stages"])
    return out


def _source_uids():
    """(启用 uid 列表, 全部 uid 列表)。用于两个口径的通过率分母：
    启用口径回答"我开着的源里有多少能用"，全量口径回答"这包里的源整体如何"。"""
    try:
        from engine.source_mgr import load_all
        allsrc = load_all() or []
    except Exception:
        return [], []
    return ([s.get("uid") or "" for s in allsrc if s.get("enabled", True)],
            [s.get("uid") or "" for s in allsrc])


def _enabled_uids():
    return _source_uids()[0]


def summary_of(items, enabled_uids=None, all_uids=None):
    """汇总：分类计数 + **两个口径**的通过率。

    - pass_rate：通过数 / 启用源数（界面默认展示，回答"我开的源里能用几个"）
    - pass_rate_all：通过数 / 全部源数（回答"整包源的整体可用度"）
    never_attempted 分别给出启用口径与全量口径——没验证过的既不算通过也不会被藏起来。
    注意：结果按 uid 合并，若 sources 目录里存在**两个文件同一 uid**（实测有 4 组），
    它们会折叠成一条记录，因此"已记录条数"可能小于文件数。
    """
    counts = {}
    for it in items:
        counts[it.get("status", "unknown")] = counts.get(it.get("status", "unknown"), 0) + 1
    if enabled_uids is None or all_uids is None:
        _en, _all = _source_uids()
        enabled_uids = enabled_uids if enabled_uids is not None else _en
        all_uids = all_uids if all_uids is not None else _all
    known = {it.get("uid") or "" for it in items}
    never_enabled = [u for u in enabled_uids if u and u not in known]
    never_all = [u for u in all_uids if u and u not in known]
    verified = counts.get("verified", 0)
    # 口径必须分子分母一致：启用口径只数**启用源里**验过的通过数，
    # 否则跑了一遍"含停用源"的批次会出现 21/6=350% 这种荒唐数字（实测踩到）
    _enabled_set = {u for u in enabled_uids if u}
    verified_enabled = sum(1 for it in items
                           if it.get("status") == "verified"
                           and (it.get("uid") or "") in _enabled_set)
    return {
        "enabled_total": len(enabled_uids),
        "all_total": len(all_uids),
        "verified": verified,
        "verified_enabled": verified_enabled,
        "partial": counts.get("partial", 0),
        "failed": counts.get("failed", 0),
        "unsupported": counts.get("unsupported", 0),
        "skipped": counts.get("skipped", 0),
        "recorded": len(items),
        "never_attempted": len(never_enabled),
        "never_attempted_all": len(never_all),
        "pass_rate": (round(verified_enabled * 100 / len(enabled_uids))
                      if enabled_uids else 0),
        "pass_rate_all": (round(verified * 100 / len(all_uids)) if all_uids else 0),
    }


def results_payload():
    """历史累计结果（按 uid 合并，跑第二批不会把第一批的结论冲掉）+ 汇总。"""
    data = _results if _results is not None else load_results()
    items = (data or {}).get("items") or []
    counts = {}
    for it in items:
        counts[it.get("status", "unknown")] = counts.get(it.get("status", "unknown"), 0) + 1
    return {
        "tested_at": (data or {}).get("tested_at", ""),
        "keyword": (data or {}).get("keyword", ""),
        "counts": counts,
        "total": len(items),
        "items": items,
        "summary": summary_of(items),
    }


def load_results():
    try:
        with open(VERIFIED_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _save(data):
    """按 uid 合并历史结果：分批验证时第二批不得把第一批的结论冲掉。"""
    global _results
    old = load_results() or {}
    merged = {}
    for it in (old.get("items") or []):
        uid = it.get("uid") or ""
        if uid:
            merged[uid] = it
    new_uids = []
    for it in (data.get("items") or []):
        uid = it.get("uid") or ""
        if not uid:
            continue
        prev = merged.get(uid)
        # "本轮跳过"（最近已验证通过/已停用）**不许覆盖**已有结论——
        # 否则分批跑第二轮时，台账会把"已验证"退回"待验证"，用户看到的是"越跑越差"
        # （实测踩到：4 个源被 skipped 记录盖掉了已验证结论）。
        if (it.get("status") == "skipped" and prev
                and prev.get("status") in ("verified", "partial", "failed", "unsupported")):
            prev = dict(prev)
            prev["skipped_at"] = it.get("tested_at") or ""
            prev["skipped_reason"] = it.get("reason") or ""
            merged[uid] = prev
            continue
        merged[uid] = it
        new_uids.append(uid)
    out = dict(data)
    out["items"] = list(merged.values())
    # last_batch = 本轮真正去验的源；调用方没给就退化成"本轮写入过的源（不含 skipped）"
    if not out.get("last_batch"):
        out["last_batch"] = [it.get("uid") for it in (data.get("items") or [])
                             if it.get("uid") and it.get("status") != "skipped"]
    return _write_merged(out)


def _write_merged(out):
    global _results
    os.makedirs(os.path.dirname(VERIFIED_FILE) or ".", exist_ok=True)
    _atomic_write(VERIFIED_FILE, out)
    _results = out
    return out


def status():
    with _lock:
        return dict(_job)


def _verified_recently(uid, days, known, now_ts=None):
    """该源是否在最近 days 天内已被验证通过（用于分批跑时不重复啃同一个源）。"""
    it = known.get(uid) or {}
    if it.get("status") != "verified":
        return False
    try:
        t = time.mktime(time.strptime(it.get("tested_at") or "", "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return False
    return (now_ts if now_ts is not None else time.time()) - t < days * 86400


def _tested_recently(uid, days, known, now_ts=None):
    """该源是否在最近 days 天内**验过**（无论结果如何）——用于中断后续跑不重复啃。"""
    it = known.get(uid) or {}
    if not it.get("tested_at"):
        return False
    try:
        t = time.mktime(time.strptime(it["tested_at"], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return False
    return (now_ts if now_ts is not None else time.time()) - t < days * 86400


def start(keyword=None, uids=None, limit=None, include_disabled=False, offset=0,
          skip_verified_days=7, skip_tested_days=0, budget=None, now=_now):
    """启动一次逐源验证（后台线程）。

    - offset：分批跑用，跳过队列前面的 offset 个源（此前没这个参数时，
      "再跑一批"永远从第一个源开始，后面的源永远轮不到——实测发现的缺陷）；
    - skip_verified_days：最近 N 天内已**验证通过**的源本轮跳过并如实记为 skipped，
      不重复占用网络；
    - 并发调用不会重复跑：返回 already_running。
    """
    global _job
    from engine.source_mgr import load_all

    raw = load_all() or []
    all_src = list(raw)
    if uids:
        want = set(uids)
        all_src = [s for s in all_src if (s.get("uid") or "") in want]
    disabled_n = 0
    if not include_disabled:
        kept = [s for s in all_src if s.get("enabled", True)]
        disabled_n = len(all_src) - len(kept)
        all_src = kept
    # 同 uid 多文件（配置重复）只验一份：逐源验证会对每个源发请求，重复跑同一
    # 站点既白花时间，也让"共 N 个源"与实际不符（实测 34 文件 / 30 uid）。
    _uniq, _seen_uid = [], set()
    for s in all_src:
        u = s.get("uid") or ""
        if u and u in _seen_uid:
            continue
        if u:
            _seen_uid.add(u)
        _uniq.append(s)
    all_src = _uniq
    lim = limit if isinstance(limit, int) and limit > 0 else DEFAULT_LIMIT
    lim = min(lim, MAX_LIMIT)

    known = {it.get("uid") or "": it for it in (load_results() or {}).get("items", [])}
    recent, pool = [], []
    for s in all_src:
        uid = s.get("uid") or ""
        if _verified_recently(uid, skip_verified_days, known) or \
                (skip_tested_days > 0 and _tested_recently(uid, skip_tested_days, known)):
            recent.append(s)
        else:
            pool.append(s)
    off = offset if isinstance(offset, int) and offset > 0 else 0
    # 排序：**从未验证过的源优先**，其次才是需要复验的（失败/部分通过）。
    # 否则失败源永远排在最前，反复点"再跑一批"也轮不到后面的源（实测发现的缺陷）。
    never = [s for s in pool if (s.get("uid") or "") not in known]
    retry = [s for s in pool if (s.get("uid") or "") in known]
    ordered = never + retry
    targets = ordered[off:off + lim]

    with _lock:
        if _job.get("status") == "running":
            return {"started": False, "already_running": True, **_job}
        _job = {"status": "running", "done": 0, "total": len(targets) + len(recent),
                "current": "", "keyword": keyword or "", "started_at": now(),
                "finished_at": "", "error": "", "skipped_disabled": disabled_n,
                "skipped_verified": len(recent), "offset": off}

    def _run():
        global _job
        items = []
        skipped_items = [{"uid": s.get("uid") or "", "name": s.get("bookSourceName") or "",
                          "status": "skipped",
                          "reason": f"最近 {skip_verified_days} 天内已验证通过，本轮不重复验证",
                          "tested_at": known.get(s.get("uid") or "", {}).get("tested_at") or now(),
                          "stages": known.get(s.get("uid") or "", {}).get("stages") or {},
                          "keyword": known.get(s.get("uid") or "", {}).get("keyword") or ""}
                         for s in recent]
        try:
            for i, src in enumerate(targets):
                nm = src.get("bookSourceName") or src.get("uid") or "?"
                with _lock:
                    _job["current"] = nm
                try:
                    items.append(verify_one(src, keyword=keyword, budget=budget))
                except BaseException as e:      # 单个源异常不得终止整轮
                    items.append({"uid": src.get("uid") or "", "name": nm,
                                  "status": "failed",
                                  "reason": f"{type(e).__name__}: {str(e)[:100]}",
                                  "tested_at": now(), "stages": {}})
                with _lock:
                    _job["done"] = len(recent) + i + 1
                # 每源落盘：长批次（几十个源、数分钟）被系统杀掉时，
                # 已经验完的结论必须留下来——否则整轮白跑（实测踩到）
                try:
                    _save({"tested_at": now(), "keyword": keyword or "",
                           "profile": os.environ.get("WR_PROFILE", "desktop"),
                           "last_batch": [x.get("uid") for x in targets],
                           "items": items + skipped_items})
                except Exception as _e:
                    print(f"[verify] 增量落盘失败: {type(_e).__name__}: {_e}", flush=True)
            payload = {"tested_at": now(), "keyword": keyword or "",
                       "profile": os.environ.get("WR_PROFILE", "desktop"),
                       "last_batch": [x.get("uid") for x in targets],
                       "items": items + skipped_items}
            _save(payload)
            with _lock:
                _job["status"] = "done"
                _job["finished_at"] = now()
                _job["current"] = ""
        except BaseException as e:
            with _lock:
                _job["status"] = "error"
                _job["error"] = f"{type(e).__name__}: {str(e)[:120]}"
                _job["finished_at"] = now()

    th = threading.Thread(target=_run, daemon=True, name="source-verify")
    th.start()
    return {"started": True, **status()}


def reset_for_tests():
    """仅供测试：清掉内存态（不删结果文件）。"""
    global _job, _results
    with _lock:
        _job = {"status": "idle", "done": 0, "total": 0, "current": "", "keyword": "",
                "started_at": "", "finished_at": "", "error": ""}
    _results = None
