# -*- coding: utf-8 -*-
"""server.manga_api —— 漫画域 API（搜索/详情/阅读/下载/书库）。Blueprint: manga"""
import os
import re
import json
import time
import unicodedata
import threading
import contextlib

from flask import Blueprint, jsonify, request, abort, Response

from engine.app_utils import _read_json, update_json, atomic_write
from engine.config import (MANGA_DIR, MANGA_LIBRARY_FILE, MANGA_HISTORY_FILE,
                           MANGA_FAV_FILE, MANGA_CACHE_DIR,
                           MANGA_DOWNLOADS_DIR, MANGA_STATE_DIR,
                           MANGA_DETAIL_CACHE_TTL, MANGA_PAGE_SIZE)
from engine.urlsec import safe_target_url as _safe_target_url, SSRFBlocked

from server.state import (
    _err_response, _err_json, _safe_seg, _safe_str_arg, _safe_int_arg, _safe_comic_id,
    _norm_comic_id, _SAFE_SEG_RE, _manga_adapter, _manga_read_adapter,
    _manga_read_images,
    _load_manga_adapters, _manga_dl, _manga_dl_key,
    _manga_adapter_name, _scan_downloaded_chapters, _manga_media_root,
    _local_chapter_images, _get_chapter_images, _manga_search_cached,
    _manga_search_singleflight,
    _prefetch_next_chapter_images,
    _integrity_summary, _manga_check_state, _manga_check_lock,
    _manga_check_one, _manga_check_worker, _mcache,
    _detail_fetch_singleflight, _detail_refresh_async, DetailUnavailable,
    _sort_split_chapters,
    _manga_search_cache, _MANGA_SEARCH_LOCK, _MANGA_SEARCH_TTL,
    _CHAPTER_IMAGES_CACHE, _CHAPTER_IMAGES_TTL, _known_chapter_pages,
    _local_cover_path, _save_cover_bytes, _manga_total_chapters,
    _manga_cached_chapters,
    _manga_stats_get, _manga_stats_request, _manga_stats_note_change,
    manga_library_revision,
    device_offline_hint,
)

bp = Blueprint("manga", __name__)


def _img_fail_hint(exc):
    """章节图片获取失败的**可行动原因**（磁盘类/网络类分别说明，其余保持原样）。
    只输出固定中文（脱敏）；异常细节由 _err_response 记进服务端日志。"""
    from engine.app_utils import disk_error_text
    from engine.neterr import classify, reason_for
    why = disk_error_text(exc)
    if not why:
        kind = classify(exc)
        why = reason_for(kind) if kind else ""
    return ("章节图片获取失败：" + why) if why else "章节图片获取失败"


@bp.route("/api/manga/sources")
def api_manga_sources():
    from engine.manga.manager import list_adapters
    from server import capabilities as _cap
    _load_manga_adapters()
    # R49i: 拷贝漫画只保留网页端——APP 通道当前被 210 IP 风控(换域名无效,
    # 需正版客户端+等待1小时),网页端搜索/下载全可用。
    # 已下载的 copymanga(APP)数据仍可本地阅读(见 detail/urls/img 本地兜底)。
    #
    # 2026-09-13（阶段 C）：这条"只保留网页端"的取舍以**网页通道真的可用**为前提。
    # 当运行时没有 Playwright/浏览器（移动端第一版就是如此）时，隐藏 APP 通道
    # 等于把"唯一可能可用的通道"藏掉，还会让前端以为网页端可用——正是设计 B2
    # 禁止的静默假兼容。因此改为**能力感知**：网页通道不可用时如实列出 APP 通道，
    # 并给每个源附 supported/degraded/unsupported 与原因。
    _caps = _cap.probe()
    _web_ok = bool(_caps.get("playwright", {}).get("available")
                   and _caps.get("playwright_browser", {}).get("available"))
    # 实测结论（engine/manga/verify.py 落盘）：与依赖级判定分开呈现——
    # status 回答"依赖够不够"，verify 回答"真的跑通过没有"（verified 不再恒为 False）
    try:
        from engine.manga import verify as _mv
        _vr = {it.get("key"): it for it in (_mv.load_results() or {}).get("items", [])}
    except Exception:
        _vr = {}
    out = []
    for _s in list_adapters():
        _k = _s.get("key")
        if _k == "copymanga" and _web_ok:
            continue                      # 桌面（网页通道可用）：行为与改造前一致
        _st = _cap.source_status(_k, _caps)
        _v = _vr.get(_k) or {}
        # 能力元信息按**类**读，不实例化（见 engine.manga.manager.adapter_meta 的注释）
        try:
            from engine.manga.manager import adapter_meta as _ameta
            _meta = _ameta(_k)
        except Exception:
            _meta = {"scrambled": False, "process_version": 0, "supports_order": False}
        # 说明文字统一由 capabilities.combined_reason 拼（已知条件 + 依赖原因）：
        # 此前这里直接用 _st["reason"]，于是"源站已迁域并启用挑战门"这类实测结论
        # 从来没露到界面上（只有笼统的"缺 curl_cffi"）。
        out.append(dict(_s, status=_st["status"],
                        reason=_cap.combined_reason(_st) or _st["reason"],
                        # 混淆源（jm 等）需要服务器做块还原：App 据此给出"重建图片缓存"入口
                        scrambled=bool(_meta.get("scrambled")),
                        process_version=int(_meta.get("process_version") or 0),
                        # 本源是否支持排序参数（App 据此决定要不要显示排序选择）
                        supports_order=bool(_meta.get("supports_order")),
                        verified=_v.get("status") == "verified",
                        verify={"status": _v.get("status", "pending"),
                                "reason": _v.get("reason", ""),
                                "tested_at": _v.get("tested_at", ""),
                                "stages": _v.get("stages", {})},
                        transport=_st.get("transport", {}),
                        channel=("web" if _k == "copymanga_web" else "app")))
    # 目录字段合并：App 用同一个端点就能拿到"移动可用性分类 + 失败阶段 + 实测时间"，
    # 避免"依赖判定/实测/分类"分散在多个接口各说一套（路线 §4.2 唯一事实来源）。
    # 完整矩阵（含未注册台账）在 /api/manga/catalog。
    try:
        from server import manga_catalog as _mc
        _rows = {r["key"]: r for r in (_mc.build() or {}).get("sources", [])}
        for _e in out:
            _r = _rows.get(_e.get("key"))
            if not _r:
                continue
            _e["category"] = _r.get("category")
            _e["category_label"] = _r.get("category_label")
            _e["category_reason"] = _r.get("category_reason")
            _e["verification"] = _r.get("verification")
            _e["supports_order"] = (_r.get("capabilities") or {}).get("supports_order",
                                                                     _e.get("supports_order"))
    except Exception as _e2:
        print(f"[manga] 目录合并失败（不影响源清单本身）: {_e2}", flush=True)
    return jsonify({"sources": out,
                    "capabilities": _cap.summary(_caps),
                    "web_channel_available": _web_ok})


@bp.route("/api/manga/copymanga/token", methods=["GET", "POST"])
def api_manga_copymanga_token():
    """配置拷贝漫画登录 Token（authorization: Token xxx，降低风控权重）
    从 2026copy.com 网页版登录态（localStorage token）获取。"""
    ad = _manga_adapter("copymanga")
    if not ad:
        return _err_json("copymanga 源不可用", 404)
    # P3-1: R59 后 "copymanga" 键经 engine.manga.manager._APP_FORBIDDEN
    # 映射到网页适配器（copymanga_web），其无 _token/_save_state 属性，
    # 直接访问会 AttributeError → 500。前端仍在调用本端点
    # （templates/manga.html setCopymangaToken、Android Repository.kt），
    # 故不删端点，改为 hasattr 探测：不支持时返回 410 + 说明文案。
    if not (hasattr(ad, "_token") and hasattr(ad, "_save_state")):
        return jsonify({
            "error": "拷贝漫画 APP 通道已停用（R59），现走网页渠道，"
                     "匿名即可搜索/下载，无需配置 Token",
            "token_set": False}), 410
    if request.method == "GET":
        return jsonify({"token_set": bool(ad._token)})
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    if not token:
        return _err_json("缺少 token")
    ad._token = token
    ad._save_state()
    print("[copymanga] 已配置登录 Token（authorization: Token xxx）", flush=True)
    return jsonify({"ok": True})


@bp.route("/api/manga/search")
def api_manga_search():
    q = _safe_str_arg("q")
    source = _safe_str_arg("source", maxlen=50)
    page = _safe_int_arg("page")
    order = _safe_str_arg("order", maxlen=10)  # 排序（jm: mr/mv/mp/tf/tr/md/pa）
    if not q:
        return _err_json("缺少关键词")
    _cache_key = (q, source, page, order)
    # P2-1: 单飞合并——并发同关键词只跑一遍全源搜索，跟随者等结果后命中
    _dev_off = device_offline_hint()
    try:
        payload, cached = _manga_search_singleflight(
            _cache_key,
            lambda: _do_manga_search(q, source, page, order, _dev_off))
    except RuntimeError as _e:
        # B05: 等待同轮搜索超时——明确"暂不可用"，不绕过 leader 自行回源
        return _err_json(str(_e), 503)
    payload["cached"] = cached
    return jsonify(payload)


# P0-1: 按源分级 deadline——网页渲染源(copymanga_web 等)冷启动+渲染常需
# 20-40s，但全源搜索最坏等待须压进前端 30s 取消窗口（原 75s 会白屏 75s），
# 故 75s → 25s；其余源 20s 足够
_MANGA_SLOW_KEYS = ("copymanga_web", "copymanga")
_MANGA_SLOW_DEADLINE = 25
_MANGA_FAST_DEADLINE = 20
# 2026-09-14：实测"每次搜索都正好 20.0s"——不是所有源都慢，而是**一个不可达的源
# （nhentai）拖满了它的 20s 期限**，用户在已经拿到其它源结果的情况下还得干等。
# 因此加两条：
#   · 耐心上限：只要**已有源返回结果**，最多再等这么久就带着部分结果返回
#     （慢源在 errors 里如实标注，不静默当成"没结果"）；
#   · 部分结果短缓存：带 errors 的结果也缓存一小段，连续重搜/翻页不必再等一轮。
_MANGA_PATIENCE = 6.0
_MANGA_PARTIAL_TTL = 60
# 2026-09-15（真断网）：本机连不出去时**不要再干等源站超时**。设备实测（飞行模式）：
# 6 个漫画源里 5 个 1s 内就因解析失败报错，剩下的禁漫/拷贝却要拖满 25s 期限，
# 用户在网络早就没了的情况下还要等半分钟才等到"搜索结束"。
# 判据只认"没有路由"（engine/neterr.local_network_down：两个探测目标都报
# ENETUNREACH/EHOSTUNREACH 才算），并且至少观察 _MANGA_OFFLINE_GRACE 秒——
# 宁可不提前结束，也不误伤正常网络（有默认路由的设备不会命中这个判据）。
_MANGA_OFFLINE_GRACE = 3.0

# P0-1: 漫画源历史搜索延迟（source_key → 秒），流式搜索按此升序发射快源
# （对齐小说端 _slow_tracker.snapshot() 的排序思路；仅内存，重启清零）
_manga_search_latency = {}


def _manga_search_adapters(source):
    """搜索用适配器列表：指定源或全源。
    R22: copymanga 用快速失败实例（210 重试链 35s×N 会挂死搜索；失败即空结果）"""
    from engine.manga.manager import list_adapters
    _load_manga_adapters()
    def _get_ad(key):
        if key == "copymanga":
            return _manga_read_adapter(key)
        return _manga_adapter(key)
    if source:
        ad = _get_ad(source)
        return [ad] if ad else []
    ads = []
    for info in list_adapters():
        ad = _get_ad(info["key"])
        if ad:
            ads.append(ad)
    return ads


def _supports_order(a):
    """能力探测用签名检查，不能用 except TypeError：
    适配器内部抛 TypeError（如解析脏 JSON）会被误判为签名不匹配，
    导致再发一次完整搜索（copymanga_web 即再跑一轮 Playwright）"""
    try:
        import inspect
        return "order" in inspect.signature(a.search).parameters
    except (TypeError, ValueError):
        return False


def _manga_search_one(a, q, page, order):
    """单源单页搜索（分页由前端"加载更多"按 page 参数逐页拉取）。
    返回 (source_key, rows, err)——err 非空即失败/超时（与"真空结果"区分）"""
    try:
        # 排序参数仅 JM 适配器支持（其它源忽略 order）
        if order and _supports_order(a):
            comics = a.search(q, page, order=order)
        else:
            comics = a.search(q, page)
        out = []
        for c in comics:
            out.append({
                "id": c.id, "title": c.title, "author": c.author,
                "cover": c.cover, "tags": c.tags, "source": a.key,
                "source_name": a.name, "total": getattr(c, "total", 0)})
        return a.key, out, ""
    except Exception as e:
        # P2-8: 异常原文只进服务端日志，errors 状态通道只放脱敏文案
        print(f"[manga] {a.name} 搜索失败: {e}", flush=True)
        from engine import neterr as _ne
        # 2026-09-15（真断网）：本机没有路由时（飞行模式/无信号）**必须**说是
        # 本机网络问题。旧文案统一写"源站异常或限流"，用户离线时会得到完全
        # 错误的结论（以为是源站坏了，反复重试）。见 engine/neterr.py。
        _txt, _ = _ne.failure_text(
            a.name, e, f"{a.name} 搜索失败（源站异常或限流），请稍后重试")
        return a.key, [], _txt


def _manga_rows_to_groups(rows):
    """原始结果行 → 按 (source,id) 成组（R65 不去重设计），保持出现次序。
    R65: 搜索结果不去重——每个 (source, id) 独立完整展示。
    站内同名不同 id 是不同作品（如拷贝站"姐姐的朋友"完结版/连载版
    两个 cid），按标题合并/丢弃会让用户搜不到其中一部；前端本身
    就以 source|id 判重（manga.html appendResults），天然兼容。"""
    groups = {}
    for r in rows:
        key = (r["source"], r["id"])
        if key in groups:
            continue
        groups[key] = {"title": r["title"], "author": r["author"],
                       "cover": r["cover"], "tags": r["tags"],
                       "sources": [{k: r[k] for k in ("id", "source", "source_name")}]}
    return list(groups.values())


def _manga_page_facts(results, per_source, page):
    """分页契约：前端不再用 page*页大小 反推，改用服务端给出的事实。
    has_more = 任一源本页拉满页大小，或其源站 total 显示后面还有"""
    _has_more = False
    for _v in per_source.values():
        if _v["count"] >= MANGA_PAGE_SIZE:
            _has_more = True
            break
        if _v["total"] and page * MANGA_PAGE_SIZE < _v["total"]:
            _has_more = True
            break
    _total_hits = max((v["total"] for v in per_source.values()), default=0)
    return _has_more, _total_hits



# ── 搜索/浏览结果的封面预热（0.68.0）──────────────────────────────────
# 用户反馈"切搜索结果页时封面加载慢"：封面走 /api/manga/cover 代理（禁漫图床校验
# Referer，App 不能直连），首次请求要等源站。搜索返回后由引擎**后台**把这页的
# 封面抓进代理缓存，界面来取时就是命中缓存（秒回），也不占服务线程。
# 有界：每次最多预热 12 张、并发 3、URL 冷却 10 分钟、与代理缓存同一把并发闸。
_COVER_WARM_PROXY_MAX = 12
_COVER_WARM_PROXY_COOLDOWN = 600
_cover_proxy_warm_at = {}



def _search_warm_enabled():
    """搜索后的后台预热是否允许（WR_DISABLE_BACKGROUND / WR_BG_SEARCH_WARM 可关）。"""
    try:
        from server.runtime import worker_enabled
        return worker_enabled("search-warm")
    except Exception:
        return True


def _warm_search_details(results, source_key="", limit=3):
    """搜索返回后，后台把**前几条的详情**抓进详情缓存（0.69.0）。

    用户反馈"jm 详情页很慢"：点开搜索结果时详情页要现打源站 `/album`（实测首 6.9s、
    热 0.8–1.3s）。搜索接口本身已经知道前几条是谁，提前把详情缓存填好，点进去就是
    命中缓存。有界：最多 3 条、已有缓存跳过、经详情单飞（不会与用户点击造成重复请求）。
    """
    if not _search_warm_enabled():
        return 0
    _n = 0
    for r in (results or [])[:max(0, limit)]:
        _src = (r.get("source") or source_key or "").strip()
        _cid = str(r.get("id") or "").strip()
        if not _src or not _cid:
            continue
        _info_p = os.path.join(MANGA_CACHE_DIR, _src, _cid, "_info_full.json")
        try:
            if os.path.exists(_info_p):
                continue
        except OSError:
            pass
        try:
            _detail_refresh_async(
                _src, _cid,
                (lambda s=_src, c=_cid, p=_info_p: _refresh_detail_cache(s, c, p)),
                delay=0.3)
            _n += 1
        except Exception:
            pass
        if _n >= limit:
            break
    return _n


def _warm_proxy_covers(source, urls):
    """后台预热若干封面的**代理缓存**（去重 + 有界 + 冷却 + 单飞）。"""
    if not _search_warm_enabled():
        return False
    _u = []
    _now = time.time()
    for u in (urls or []):
        u = (u or "").strip()
        if not u.lower().startswith(("http://", "https://")):
            continue
        if u in _u:
            continue
        if _now - _cover_proxy_warm_at.get(u, 0) < _COVER_WARM_PROXY_COOLDOWN:
            continue
        _hit, _ = _cover_proxy_get(u)
        if _hit:
            continue
        _u.append(u)
        if len(_u) >= _COVER_WARM_PROXY_MAX:
            break
    if not _u:
        return False
    for u in _u:
        _cover_proxy_warm_at[u] = _now
    if len(_cover_proxy_warm_at) > 800:       # 冷却表有界
        for _k in sorted(_cover_proxy_warm_at,
                         key=lambda k: _cover_proxy_warm_at[k])[:300]:
            _cover_proxy_warm_at.pop(_k, None)

    def _work():
        from engine.manga.downloader import fetch_image_checked
        from engine.manga.verify import sniff_image
        ad = _manga_read_adapter(source) or _manga_adapter(source)
        if ad is None:
            return
        for u in _u:
            if not _COVER_PROXY_SEM.acquire(timeout=0.2):
                return
            try:
                _hit2, _ = _cover_proxy_get(u)
                if _hit2:
                    continue
                _h = {}
                try:
                    _h = dict(ad.image_headers(u) or {})
                except Exception:
                    _h = {}
                _h.setdefault("User-Agent",
                              "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0")
                _r = fetch_image_checked(u, _h, timeout=12)
                _d = getattr(_r, "content", b"") or b""
                if int(getattr(_r, "status_code", 0) or 0) == 200 and \
                        len(_d) > 1000 and sniff_image(_d):
                    _cover_proxy_put(u, _d)
            except Exception:
                pass
            finally:
                try:
                    _COVER_PROXY_SEM.release()
                except Exception:
                    pass

    threading.Thread(target=_work, daemon=True,
                     name="cover-proxy-warm").start()
    return True


def _do_manga_search(q, source, page, order="mr", device_offline=None):
    """漫画搜索核心（可缓存）：指定源或全源并行。
    R65: 不去重——每个 (source, id) 独立成条；同名不同 id 视为不同作品全保留

    device_offline：客户端给的**设备级**联网信号（True=本机没有可用网络，
    None=未知/在线）。只用来决定措辞与是否继续干等，不决定是否搜索。"""
    ads = _manga_search_adapters(source)
    results = []
    _per_source = {}   # source_key -> {total, count}（分页判定的事实源）
    import concurrent.futures as _cf
    _errors = {}   # source_key -> 失败/超时原因（与"真空结果"区分）
    # 断网判定的状态**必须在 if ads 之外初始化**：指定源不可用/适配器缺依赖时
    # _manga_search_adapters 会返回空列表，而这里的状态在分支外仍要参与返回
    # （2026-09-15 评审抓到：只在 if ads 内初始化 → 空源时 UnboundLocalError）
    _t_start = time.time()
    _patience_hit = False
    _offline_hit = False
    _probe = {"done": False, "down": False}
    from engine import neterr as _ne

    def _probe_down():
        if not _probe["done"]:
            _probe["down"] = _ne.local_network_down()
            _probe["done"] = True
        return _probe["down"]

    def _offline_evidence():
        """设备级证据：客户端信号或直连探测（逐源失败文字**不算**，见 engine/neterr.py）"""
        return bool(device_offline) or _probe_down()

    if ads:
        # 这里刻意用 `ex = ...` + finally 里的 `shutdown(wait=False)`，**不用**
        # `with ThreadPoolExecutor(...)`：`with` 退出等价于 `shutdown(wait=True)`，
        # 会把下面算出来的期限作废（一旦有 worker 卡住，响应就再也回不去）。
        # 实测确认（2026-09-17）：`with` + 内部 shutdown(wait=False) 退出仍会等满
        # worker 的时长，所以这不是风格问题，是正确性问题。
        #
        # 另外 `cancel_futures=True`：期限到点时**还没开跑**的源直接取消，不再占线程。
        ex = _cf.ThreadPoolExecutor(max_workers=min(len(ads), 8))
        try:
            # R66: 超时按源区分（P0-1: 慢源 75s→25s，见模块级常量注释）
            _now = time.time()
            _deadline = {a.key: _now + (_MANGA_SLOW_DEADLINE
                                        if a.key in _MANGA_SLOW_KEYS
                                        else _MANGA_FAST_DEADLINE)
                         for a in ads}
            futs = {ex.submit(_manga_search_one, a, q, page, order): a for a in ads}
            _pending = set(futs)
            while _pending:
                _rem = min(_deadline[futs[f].key] for f in _pending) - time.time()
                if _rem <= 0:
                    break
                # 已有结果 → 不再为一个慢源把用户晾在 20s 上（耐心上限）
                if results and time.time() - _t_start >= _MANGA_PATIENCE:
                    _patience_hit = True
                    break
                # 真断网：本机确实连不出去 → 立刻收尾（剩余源如实标注），
                # 不再干等 20/25s 的源站期限
                if (not results
                        and time.time() - _t_start >= _MANGA_OFFLINE_GRACE
                        and _offline_evidence()):
                    _offline_hit = True
                    break
                # 等待粒度 0.5s：既不影响"有源完成就立刻继续"，又能让上面的
                # 耐心上限**及时**生效（用 5s 粒度时，已有结果也要等到 5s 才发现）
                _done, _pending = _cf.wait(
                    _pending, timeout=min(_rem, 0.5),
                    return_when=_cf.FIRST_COMPLETED)
                for f in _done:
                    _key, _rows, _err = f.result()
                    if _err:
                        _errors[_key] = _err
                        continue
                    results.extend(_rows)
                    # 每源独立记录：源站总数 + 本页实际条数。
                    # 多源下单页可返回 页大小×源数 条，用全局 max(total)
                    # 配合 page*30 反推会在多源/末页/去重时全面失准
                    _per_source[_key] = {
                        "total": max((int(r.get("total") or 0) for r in _rows),
                                     default=0),
                        "count": len(_rows)}
            # 未完成的任务：如实记入 errors（不静默当"没结果"）
            #   耐心上限提前返回 → "仍在查询"，与真超时区分开
            #   到了自己的期限 → "搜索超时"
            for f in _pending:
                _a = futs[f]
                _errors.setdefault(
                    _a.key,
                    (f"{_a.name} 搜索失败（{_ne.REASON_UNREACHABLE}），"
                     f"本机没有可用网络，已提前结束等待"
                     if _offline_hit else
                     f"{_a.name} 仍在查询（已先显示其它源的结果，稍后重试可补上）"
                     if _patience_hit else
                     f"{_a.name} 搜索超时（源站无响应或服务器繁忙），请稍后重试"))
        finally:
            # cancel_futures：还没开跑的直接取消；已在跑的 worker 留在后台自己收尾
            # （它们不再影响本次响应，结果由 requests 缓存/下次搜索兜住）
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:                    # 老版本没有 cancel_futures
                ex.shutdown(wait=False)
    glist = _manga_rows_to_groups(results)
    # 顺序 = 结果出现次序（同一源的多条保持站方返回顺序；多源时按源首次出现排序）
    _src_order = {}
    for r in results:
        _src_order.setdefault(r["source"], len(_src_order))
    glist.sort(key=lambda g: _src_order.get(g["sources"][0]["source"], 1e9))
    # raw_count = 本页源站原始条数（R65 后不再做标题合并，len(results) 即原始条数）
    _has_more, _total_hits = _manga_page_facts(results, _per_source, page)
    # 后台预热本页封面的代理缓存（用户反馈"切页封面加载慢"）——不阻塞本次响应
    try:
        _warm_proxy_covers(source if source else "",
                           [r.get("cover") or "" for r in results[:24]])
    except Exception:
        pass
    # 后台预热前几条的**详情**（用户反馈"jm 详情页慢"）：点进去就是命中缓存
    try:
        _warm_search_details(results, source if source else "", limit=3)
    except Exception:
        pass
    if not ads:
        # 指定源不存在/适配器缺依赖/全部源被停用：这是**业务结果**，不是异常。
        # 如实说明"没有可用的源"，不要让前端拿到空 errors 去猜（评审要求：
        # 空源、未知源、缺依赖都要有明确结果）
        _errors["_sources"] = (
            f"没有可用的漫画源（指定源：{source or '全部'}）——"
            f"源可能未注册、被停用或缺少运行依赖，见「设置 → 漫画源」")
    _net_down = bool(ads) and (_offline_hit or _ne.round_is_offline(
        bool(results), list(_errors.values()), device_offline=device_offline))
    return {"results": glist, "sources": len(ads), "page": page,
            "q": q, "source": source,
            "page_size": MANGA_PAGE_SIZE,
            "raw_count": len(results),
            "per_source_total": _per_source,
            "has_more": _has_more,
            # R66: 失败/超时的源单独列出（区别于"真没结果"），前端可提示重试
            "errors": _errors,
            # 2026-09-15: 整轮无结果且本机连不出去 → 前端据此说"本机没网"，
            # 而不是让用户以为源站都坏了（见 engine/neterr.py）
            "network_down": _net_down,
            # 兼容旧客户端（Android 内嵌页）：保留单一总数字段
            "total_hits": _total_hits}


@bp.route("/api/manga/search/stream")
def api_manga_search_stream():
    """漫画流式搜索（SSE）：每个源完成即推送该源结果，快源（历史延迟低）先出。
    P0-1: 对齐小说端 /api/search/stream 模式——漫画按源分组天然成立，
    无需小说端的分组归并。响应格式：text/event-stream，
    事件 data: JSON：
      进度事件 {groups, source, source_name, err, done, total, elapsed, finished:false}
      最终事件 {groups:[], done, total, elapsed, finished:true,
                errors, has_more, per_source_total, total_hits,
                raw_count, page, page_size, sources}
      缓存命中只发一条 {finished:true, cached:true, groups: 全量, ...}
    """
    q = _safe_str_arg("q")
    source = _safe_str_arg("source", maxlen=50)
    page = _safe_int_arg("page")
    order = _safe_str_arg("order", maxlen=10)  # 排序（jm: mr/mv/mp/tf/tr/md/pa）
    if not q:
        return _err_json("缺少关键词")
    _cache_key = (q, source, page, order)
    # 搜索缓存命中 → 一次性推全量（瞬时秒回）。
    # 命中判定与 _manga_search_cached 一致（TTL + 空结果不命中）；
    # 这里只读不写，直接读缓存避免 builder 空跑
    _now = time.time()
    with _MANGA_SEARCH_LOCK:
        _hit = _manga_search_cache.get(_cache_key)
    if _hit and _now - _hit[0] < _MANGA_SEARCH_TTL \
            and (_hit[1] or {}).get("results"):
        _payload = _hit[1]
        def _cached_gen():
            yield "data: " + json.dumps({
                "groups": _payload.get("results", []),
                "done": _payload.get("sources", 0),
                "total": _payload.get("sources", 0),
                "elapsed": 0,
                "finished": True,
                "cached": True,
                "errors": _payload.get("errors", {}),
                "network_down": _payload.get("network_down", False),
                "has_more": _payload.get("has_more", False),
                "per_source_total": _payload.get("per_source_total", {}),
                "total_hits": _payload.get("total_hits", 0),
                "raw_count": _payload.get("raw_count", 0),
                "page": _payload.get("page", page),
                "page_size": _payload.get("page_size", MANGA_PAGE_SIZE),
                "sources": _payload.get("sources", 0),
            }, ensure_ascii=False) + "\n\n"
        return Response(_cached_gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    ads = _manga_search_adapters(source)
    _dev_off = device_offline_hint()

    def _gen():
        import concurrent.futures as _cf
        t0 = time.time()
        total = len(ads)
        done_count = 0
        results = []
        _per_source = {}
        _errors = {}
        # 快源优先：按历史延迟升序发射（无记录源默认 5s 排中间）
        ads.sort(key=lambda a: _manga_search_latency.get(a.key, 5.0))
        _t_start = time.time()
        _offline_hit = False
        _probe = {"done": False, "down": False}
        from engine import neterr as _ne
        def _probe_down():
            if not _probe["done"]:
                _probe["down"] = _ne.local_network_down()
                _probe["done"] = True
            return _probe["down"]

        def _offline_evidence():
            return bool(_dev_off) or _probe_down()

        _deadline = {a.key: _t_start + (_MANGA_SLOW_DEADLINE
                                        if a.key in _MANGA_SLOW_KEYS
                                        else _MANGA_FAST_DEADLINE)
                     for a in ads}
        ex = _cf.ThreadPoolExecutor(max_workers=max(min(len(ads), 8), 1))
        try:
            futs = {ex.submit(_manga_search_one, a, q, page, order): a
                    for a in ads}
            _pending = set(futs)
            _patience_hit = False
            while _pending:
                _rem = min(_deadline[futs[f].key] for f in _pending) - time.time()
                if _rem <= 0:
                    break
                # 与阻塞端点同一口径：已有结果就不为慢源把流一直挂着
                # （客户端早就拿到快源结果了，但流不关会让"搜索中"状态和汇总行
                #   一直等到 20s——实测首屏 580ms、汇总却要 20s）
                if results and time.time() - t0 >= _MANGA_PATIENCE:
                    _patience_hit = True
                    break
                # 与阻塞端点同一口径：真断网时提前收流（否则界面要等满 25s）
                if (not results
                        and time.time() - t0 >= _MANGA_OFFLINE_GRACE
                        and _offline_evidence()):
                    _offline_hit = True
                    break
                _done, _pending = _cf.wait(
                    _pending, timeout=min(_rem, 0.5),
                    return_when=_cf.FIRST_COMPLETED)
                for f in _done:
                    _a = futs[f]
                    _key, _rows, _err = f.result()
                    done_count += 1
                    # 记录源延迟：完成时刻即该源本次耗时（全部源同时发射）
                    _manga_search_latency[_key] = round(time.time() - t0, 2)
                    _groups = []
                    if _err:
                        _errors[_key] = _err
                    else:
                        results.extend(_rows)
                        _per_source[_key] = {
                            "total": max((int(r.get("total") or 0)
                                          for r in _rows), default=0),
                            "count": len(_rows)}
                        _groups = _manga_rows_to_groups(_rows)
                    # 每个源完成都发事件（失败源 err 非空、groups 为空，
                    # 前端可即时提示该源失败而非干等）
                    yield "data: " + json.dumps({
                        "groups": _groups,
                        "source": _key,
                        "source_name": _a.name,
                        "err": _err or "",
                        "done": done_count,
                        "total": total,
                        "elapsed": round(time.time() - t0, 1),
                        "finished": False,
                    }, ensure_ascii=False) + "\n\n"
            # 未完成的源：记入 errors 并补发进度事件（不静默当"没结果"）
            #   已有结果时提前收流 → 文案是"仍在查询"，与真超时区分
            for f in _pending:
                _a = futs[f]
                _err = ((f"{_a.name} 搜索失败（{_ne.REASON_UNREACHABLE}），"
                         f"本机没有可用网络，已提前结束等待"
                         if _offline_hit else
                         f"{_a.name} 仍在查询（已先显示其它源的结果，稍后重试可补上）"
                         if _patience_hit else
                         f"{_a.name} 搜索超时（源站无响应或服务器繁忙），"
                         f"请稍后重试"))
                _errors.setdefault(_a.key, _err)
                done_count += 1
                yield "data: " + json.dumps({
                    "groups": [],
                    "source": _a.key,
                    "source_name": _a.name,
                    "err": _err,
                    "done": done_count,
                    "total": total,
                    "elapsed": round(time.time() - t0, 1),
                    "finished": False,
                }, ensure_ascii=False) + "\n\n"
        finally:
            ex.shutdown(wait=False)
        # 汇总分页事实 + 写搜索缓存（空结果/带 errors 不缓存，
        # 由 _manga_search_cached 把关，与非流式端点同一语义）
        _has_more, _total_hits = _manga_page_facts(results, _per_source, page)
        from engine import neterr as _ne
        _net_down = bool(ads) and (_offline_hit or _ne.round_is_offline(
            bool(results), list(_errors.values()), device_offline=_dev_off))
        _payload = {"results": _manga_rows_to_groups(results),
                    "sources": len(ads), "page": page, "q": q, "source": source,
                    "page_size": MANGA_PAGE_SIZE, "raw_count": len(results),
                    "per_source_total": _per_source, "has_more": _has_more,
                    "errors": _errors, "total_hits": _total_hits,
                    "network_down": _net_down}
        try:
            _manga_search_cached(_cache_key, lambda: _payload)
        except Exception:
            pass
        # 最终事件：groups 为空（增量已在进度事件中推完），只带汇总字段
        yield "data: " + json.dumps({
            "groups": [],
            "done": done_count,
            "total": total,
            "elapsed": round(time.time() - t0, 1),
            "finished": True,
            "errors": _errors,
            "network_down": _net_down,
            "has_more": _has_more,
            "per_source_total": _per_source,
            "total_hits": _total_hits,
            "raw_count": len(results),
            "page": page,
            "page_size": MANGA_PAGE_SIZE,
            "sources": len(ads),
        }, ensure_ascii=False) + "\n\n"

    return Response(_gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


def _rebuild_images_impl(source, comic_id=None, chapter=None, force_all=False):
    """重建图片缓存的核心（源级/单作品级共用）。

    背景：块还原参数算错时写进缓存的图是错的（花图），而且会被一直复用。
    修好算法后不能要求用户清数据/重下整个书库，所以要能"只重建受影响的章节"。

    两个存放位置都要扫（本机有两份图片，读端优先用下载目录）：
      · MANGA_CACHE_DIR      —— 在线阅读的懒下载缓存
      · MANGA_DOWNLOADS_DIR  —— 用户下载的章节（图片按新算法重拉，**下载记录/书库条目保留**）
    """
    ad = _manga_read_adapter(source) or _manga_adapter(source)
    if ad is None:
        abort(404, "漫画源不存在")
    try:
        cur_ver = int(getattr(ad, "PROCESS_VERSION", 0) or 0)
    except (TypeError, ValueError):
        cur_ver = 0
    from engine.manga.downloader import PROCESS_MARKER
    roots = [MANGA_CACHE_DIR, MANGA_DOWNLOADS_DIR]
    removed = 0
    freed = 0
    rebuilt = []
    kept = 0
    unmarked = 0
    for root in roots:
        src_root = os.path.join(root, source)
        if not os.path.isdir(src_root):
            continue
        for cid in sorted(os.listdir(src_root)):
            if comic_id and cid != comic_id:
                continue
            comic_root = os.path.join(src_root, cid)
            if not os.path.isdir(comic_root):
                continue
            for ch in sorted(os.listdir(comic_root)):
                d = os.path.join(comic_root, ch)
                if not os.path.isdir(d):
                    continue
                if chapter and ch != chapter:
                    continue
                marker = os.path.join(d, PROCESS_MARKER)
                ver = None
                if os.path.exists(marker):
                    try:
                        with open(marker, encoding="utf-8") as f:
                            ver = ((json.load(f) or {}).get(getattr(ad, "key", ""))
                                   or {}).get("version")
                    except Exception:
                        ver = None
                if not force_all and cur_ver and ver == cur_ver:
                    kept += 1
                    continue
                if ver is None and not force_all:
                    unmarked += 1
                targets = [f for f in os.listdir(d)
                           if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp",
                                                 ".gif", ".avif"))]
                if os.path.exists(marker):
                    targets.append(PROCESS_MARKER)
                for f in targets:
                    p2 = os.path.join(d, f)
                    try:
                        sz = os.path.getsize(p2)
                        os.remove(p2)
                        removed += 1
                        freed += sz
                    except OSError:
                        pass
                try:
                    if not os.listdir(d):
                        os.rmdir(d)
                except OSError:
                    pass
                rebuilt.append(f"{cid}/{ch}")
    try:
        from server.state import _manga_stats_request
        if comic_id:
            _manga_stats_request(source, comic_id)
    except Exception:
        pass
    print(f"[manga] 重建图片缓存 {source}"
          f"{'/' + comic_id if comic_id else ''}：删 {removed} 个文件，释放 {freed} 字节，"
          f"章节 {len(rebuilt)}（当前处理版本 {cur_ver}）", flush=True)
    return {"ok": True, "removed_files": removed, "freed_bytes": freed,
            "chapters": rebuilt, "kept": kept, "unmarked": unmarked,
            "process_version": cur_ver,
            "note": ("已删除旧处理版本的图片，下次阅读会重新拉取并按新算法还原"
                     if removed else "没有需要重建的图片缓存")}


@bp.route("/api/manga/<source>/rebuild-images", methods=["POST"])
def api_manga_rebuild_images_source(source):
    """**按源**重建图片缓存（App 的"重建图片缓存"入口用它；受影响的源一次清干净）"""
    source = _safe_seg(source, "漫画源")
    force_all = request.args.get("all") in ("1", "true", "yes")
    return jsonify(_rebuild_images_impl(source, None,
                                        (request.args.get("chapter") or "").strip() or None,
                                        force_all))


@bp.route("/api/manga/<source>/<comic_id>/rebuild-images", methods=["POST"])
def api_manga_rebuild_images(source, comic_id):
    """**按作品**重建图片缓存（?chapter=<id> 只重建该话；?all=1 连当前版本也重建）"""
    source = _safe_seg(source, "漫画源")
    comic_id = _safe_comic_id(source, comic_id)
    force_all = request.args.get("all") in ("1", "true", "yes")
    return jsonify(_rebuild_images_impl(source, comic_id,
                                        (request.args.get("chapter") or "").strip() or None,
                                        force_all))


@bp.route("/api/manga/catalog")
def api_manga_catalog():
    """移动源**目录/支持矩阵**（唯一事实来源）：注册 / 依赖 / 实测 / 移动可用性分类。

    0.51.0 路线 §4.2、§4.4：代码文件存在 ≠ 会出现在 App 里 ≠ 能用；五分类必须分开，
    未注册/不可用的源保留原因（"删源让通过率变好"不是修复）。
    参数 ?format=md 直接返回可归档的 Markdown 表格。
    """
    from server import manga_catalog as _mc
    cat = _mc.build()
    if (request.args.get("format") or "").lower() in ("md", "markdown"):
        from flask import Response as _Resp
        return _Resp(_mc.render_markdown(cat), mimetype="text/markdown; charset=utf-8")
    return jsonify(cat)


@bp.route("/api/manga/sources/verify", methods=["POST"])
def api_manga_sources_verify():
    """启动漫画源功能级验证（搜索 → 详情/目录 → 章节图片 → 真实取一张图）。

    异步：202（或已有运行时 200 + already_running）；结果经
    /api/manga/sources/verify/status 轮询、/results 读取。
    依赖缺失的源由能力台账直接判 unsupported，不做"偷偷换通道再试"。
    """
    from engine.manga import verify as manga_verify
    data = request.get_json(silent=True) or {}

    def _int(v, dflt):
        try:
            return int(dflt if v is None else v)
        except (TypeError, ValueError):
            return dflt

    r = manga_verify.start(keyword=(data.get("keyword") or None),
                           keys=data.get("keys") or None,
                           limit=_int(data.get("limit"), None),
                           offset=_int(data.get("offset"), 0),
                           skip_verified_days=_int(data.get("skip_verified_days"), 7),
                           skip_tested_days=_int(data.get("skip_tested_days"), 0))
    code = 202 if r.get("started") else 200
    return jsonify({"ok": True, **r}), code


@bp.route("/api/manga/sources/verify/status")
def api_manga_sources_verify_status():
    from engine.manga import verify as manga_verify
    return jsonify({"ok": True, **manga_verify.status()})


@bp.route("/api/manga/sources/verify/results")
def api_manga_sources_verify_results():
    from engine.manga import verify as manga_verify
    return jsonify({"ok": True, **manga_verify.results_payload()})


@bp.route("/api/manga/browse")
def api_manga_browse():
    """按分类浏览漫画源（仅对**自己声明了分类**的适配器有效）。

    漫画侧没有 Legado 那种 exploreUrl，因此由适配器实现 categories()/browse()；
    没实现的源一律 404 并说明原因——不编造榜单。
    """
    source = _safe_seg(request.args.get("source", ""), "漫画源")
    category = request.args.get("category") or ""
    try:
        page = int(request.args.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    ad = _manga_read_adapter(source)
    if not ad:
        abort(404, "漫画源不存在")
    if not hasattr(ad, "categories") or not hasattr(ad, "browse"):
        return _err_json(f"该源未提供排行/分类：{source}", 404)
    cats = ad.categories() or []
    if not cats:
        return _err_json(f"该源未提供排行/分类：{source}", 404)
    if category and category not in [c.get("key") for c in cats]:
        return _err_json(f"该源没有分类 {category}", 404)
    try:
        comics = ad.browse(category or cats[0].get("key"), page) or []
    except Exception as e:
        return _err_response(e, 502, "浏览失败")
    return jsonify({"categories": cats, "category": category or cats[0].get("key"),
                    "page": page, "total": len(comics),
                    "results": [{"id": c.id, "title": c.title, "author": c.author,
                                 "cover": c.cover, "tags": c.tags,
                                 "source": source, "source_name": ad.name}
                                for c in comics]})


def _pos_chapter_label(pos):
    """从历史 `pos`（"<章名> P<页码>"）里取出章名。取不到返回空串。

    注意：本函数与其它定位辅助函数必须放在路由装饰器**之前**。上一版把它们插在
    `@bp.route("/api/manga/<source>/<comic_id>")` 与 `def api_manga_detail` 之间，
    结果装饰器套在了辅助函数上：详情路由变成 `_resume_label_from_pos(source, comic_id)`
    → 整个漫画详情 500「服务器内部错误」，书库里的漫画全部打不开（用户当场反馈
    "书库的漫画阅读全都不可用了，因为目录消失了"）。已按此结构修正。
    """
    _p = str(pos or "")
    return re.sub(r"\s*P\d+\s*$", "", _p).strip()


_LABEL_STRIP_RE = re.compile(r"[\s\u3000]+")
# 同一话的各种写法：第01話 / 第1话 / Ch.1 / 01 … 归一后只剩"可比较的主体"
_LABEL_NOISE = ("第", "話", "话", "章", "回", "集", "卷", "節", "节", "篇")
_CJK_DIGITS = {"零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "貳": 2,
               "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6,
               "陆": 6, "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9}


def _label_key(text):
    """标签归一化：全角→半角、去空白、统一"话/話"、去掉"第…话"外壳，小写。"""
    t = _LABEL_STRIP_RE.sub("", unicodedata.normalize("NFKC", str(text or "")))
    for _n in _LABEL_NOISE:
        t = t.replace(_n, "")
    return t.strip().lower()


def _label_number(text):
    """从标签里取出话号（浮点）；取不到返回 None。

    支持 "第47话"→47、"12.5"→12.5、"第十二话"→12（简单中文数字：一到九十九）。
    """
    t = unicodedata.normalize("NFKC", str(text or ""))
    m = re.search(r"(\d+(?:\.\d+)?)", t)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    # 中文数字（有界：只处理 一…九十九 ）
    _cn = "".join(ch for ch in t if ch in _CJK_DIGITS or ch == "十")
    if not _cn:
        return None
    if "十" in _cn:
        _a, _, _b = _cn.partition("十")
        _tens = _CJK_DIGITS.get(_a, 1) if _a else 1
        _ones = _CJK_DIGITS.get(_b, 0) if _b else 0
        return float(_tens * 10 + _ones)
    return float(_CJK_DIGITS.get(_cn[-1], 0))


def _resolve_reading_position(chapters, rec, downloaded=None):
    """把一条阅读记录落到**当前**章节列表的某个下标上（0.64.0 核心修复）。

    为什么不能用记录里的 `idx`：`idx` 是"当时那份目录"的下标，而目录是会变的
    （站方加更、删章、插入番外/杂图）。实测（开发机真实数据）：

        copymanga_web:woxihuanderensuoxihuanderen 记录 idx=55 pos='第47话 P30'，
        而当前目录只剩 54 章（idx 越界），且 idx=44 的标签是"第37话"——
        下标与话号本就不一一对应。

    旧实现遇到越界直接退回"第一个已下载话/第 1 话"，于是用户看到"我明明读到
    第47话，重进却从第一话开始"；更糟的是阅读器落点后会自动保存，把真实进度
    覆盖成第 1 话（用户 2026-09-17 反馈"刚读完一本，重进就让我从第一话重读"）。

    定位优先级（越靠前越可信）：
      1. `chapter_id` 完全一致 —— 章节身份，最可靠；
      2. 章名**精确**一致；
      3. 归一化后一致（全角/空格/"话|話"/"第…话"外壳差异）；
      4. **话号唯一**一致（"第47话" ↔ 目录里唯一的 47）—— 视为精确；
      5. 话号多命中/找不到 → 取话号最接近的一话（近似，带 note）；
      6. 记录没有可比对的章名时，才退回 `idx`（仍标为近似）；
      7. 都不行 → index=-1（调用方不得当成"从第一话开始"悄悄处理）。

    返回 dict：index / exact / by / record_label / matched_label / note。
    `by` 是稳定的定位方式枚举：chapter_id|label|label_key|number|near|idx|none。
    """
    _chs = [c for c in (chapters or []) if isinstance(c, dict)]
    _rec = rec or {}
    _rid = str(_rec.get("chapter_id") or "").strip()
    _rlabel = (_rec.get("chapter_label") or "").strip() \
        or _pos_chapter_label(_rec.get("pos"))
    _ridx = _rec.get("idx")
    try:
        _ridx = int(_ridx)
    except Exception:
        _ridx = None
    out = {"index": -1, "exact": False, "by": "none", "record_label": _rlabel,
           "record_id": _rid, "matched_id": "", "matched_label": "", "note": ""}
    if not _chs:
        out["note"] = "还没有本章节目录，无法定位上次读到的位置"
        return out

    def _label_of(c):
        return str(c.get("name") or c.get("label") or "").strip()

    def _hit(i, by, exact, note=""):
        return {"index": i, "exact": exact, "by": by, "record_label": _rlabel,
                "record_id": _rid, "matched_id": str(_chs[i].get("id") or ""),
                "matched_label": _label_of(_chs[i]), "note": note}

    # 1) 章节 id（身份）
    if _rid:
        for i, c in enumerate(_chs):
            if str(c.get("id") or "") == _rid:
                return _hit(i, "chapter_id", True)
    # 2) 章名精确
    if _rlabel:
        for i, c in enumerate(_chs):
            if _label_of(c) == _rlabel:
                return _hit(i, "label", True)
        # 3) 归一化一致
        _k = _label_key(_rlabel)
        if _k:
            for i, c in enumerate(_chs):
                if _label_key(_label_of(c)) == _k:
                    return _hit(i, "label_key", True)
        # 4) 话号唯一
        _num = _label_number(_rlabel)
        if _num is not None:
            _same = [i for i, c in enumerate(_chs)
                     if _label_number(_label_of(c)) == _num]
            if len(_same) == 1:
                return _hit(_same[0], "number", True)
            if _same:
                # 多个同号（同话分不同组/版本）：仍比下标可信，但要如实说明
                return _hit(_same[0], "number", False,
                            f"目录里有 {len(_same)} 个「{_rlabel}」，已定位到第一个")
            # 5) 找不到同号 → 取最接近的一话
            _nums = [(i, _label_number(_label_of(c))) for i, c in enumerate(_chs)]
            _nums = [(i, n) for i, n in _nums if n is not None]
            if _nums:
                _i, _n = min(_nums, key=lambda t: abs(t[1] - _num))
                return _hit(_i, "near", False,
                            f"上次读到的「{_rlabel}」已不在本章节目录里，"
                            f"已定位到最接近的「{_label_of(_chs[_i])}」")
    # 6) 退回下标（只在记录没有可用章名时）
    if _ridx is not None and 0 <= _ridx < len(_chs):
        return _hit(_ridx, "idx", False,
                    "旧记录只有下标（本章节目录已变化），落点可能不准")
    out["note"] = (f"上次读到的「{_rlabel}」在当前章节目录里找不到"
                   if _rlabel else "旧记录缺少章名，无法定位")
    if downloaded:
        _dl = [i for i, c in enumerate(_chs)
               if str(c.get("id") or "") in downloaded]
        if _dl:
            out["index"] = _dl[0]
            out["by"] = "downloaded"
            out["matched_label"] = _label_of(_chs[_dl[0]])
            out["note"] = f"{out['note']}；已定位到第一个已下载话"
    return out


def _resume_payload(source, comic_id, chapters, downloaded=None):
    """该作品当前的续读信息（无记录时返回 None）。只读磁盘，零源站请求。"""
    try:
        _hist = json.load(open(MANGA_HISTORY_FILE, encoding="utf-8")) or {}
    except Exception:
        _hist = {}
    _rec = _hist.get(_manga_dl_key(source, comic_id))
    if not isinstance(_rec, dict) or not _rec:
        return None
    _r = _resolve_reading_position(chapters, _rec, downloaded)
    _r["pos"] = _rec.get("pos") or ""
    # 页码：记录形如 "第23话 P30"。必须一起给出——网页端直接读 resume.page，
    # 缺了它就会"定位到正确的话、却从第 1 页开始"，随后实时保存把 P30 写成 P1。
    _m = re.search(r"P(\d+)", _r["pos"])
    _r["page"] = int(_m.group(1)) if _m else 0
    _r["ts"] = _rec.get("ts") or 0
    return _r


def _has_detail_content(data):
    """详情是否**有内容**：`chapters` **或** `volumes` 非空。

    2026-09-18 实测缺陷：只判断 `chapters` 的写法把"**只有卷的漫画**"当成风控脏数据——
    实测《巨人》(jurenmeiman) 的 5 章在源站全是 type=2（卷），于是：
      · 详情接口把它判为"章节为空"→ 又走一次 Playwright 兜底 → **24.2 秒**才返回
        （同一时刻話型漫画《魔王大人…》只用 1.0 秒）；
      · 响应还被标成 `fallback=True`（看起来像降级）；
      · `_info.json` 缓存因为 `chapters` 为空被判无效 → **每次打开都重新走一遍**。
    判据必须与"数据真的空"一致：两种章节形态都算有内容。
    """
    if not isinstance(data, dict):
        return False
    return bool(data.get("chapters") or data.get("volumes"))


@bp.route("/api/manga/<source>/<comic_id>")
def api_manga_detail(source, comic_id):
    def _ensure_identity(_payload):
        """兜底：任何详情响应都必须带 source 与 comic_id（客户端据此查阅读进度）。

        历史教训（0.63.1）：quick 路径只给 "id"，客户端 comic_id 读成空串 →
        续读落点永远是第一话。这类"少一个字段"的缺陷在四条返回路径里各写一遍
        必然复发，所以在这里统一补。

        0.64.0 再补一层：**续读落点由服务端解析**（`resume`），客户端不再自己用
        下标去猜——下标在目录变化后会指向别的一话（用户 2026-09-17 反馈）。
        """
        if isinstance(_payload, dict):
            _payload.setdefault("source", source)
            _payload.setdefault("comic_id", comic_id)
            if "chapters" in _payload and "resume" not in _payload:
                try:
                    _ch = list(_payload.get("chapters") or []) + \
                        list(_payload.get("volumes") or [])
                    _res = _resume_payload(source, comic_id, _ch,
                                           set(_payload.get("downloaded") or []))
                    if _res is not None:
                        _payload["resume"] = _res
                except Exception as _e:
                    print(f"[manga-detail] 续读解析失败（忽略）: "
                          f"{type(_e).__name__}: {_e}", flush=True)
        return _payload


    source = _safe_seg(source, "漫画源")
    comic_id = _safe_comic_id(source, comic_id)
    ad = _manga_adapter(source)
    # R49i: copymanga(APP)已停用——但已下载数据必须可读:
    # 本地有 _info.json 时(quick 路径)不依赖适配器
    _dl_info = os.path.join(MANGA_DOWNLOADS_DIR, source, comic_id, "_info.json")
    _dl_ok = os.path.exists(_dl_info)
    if not ad and not _dl_ok:
        abort(404, "漫画源不存在")
    # 本地已下载快速路径：downloads/_info.json 有章节列表 → 秒回本地元数据，
    # 完整详情（简介/推荐等）后台刷新——避免冷启动 Playwright 渲染 15s 白屏
    if os.path.exists(_dl_info):
        try:
            _li = json.load(open(_dl_info, encoding="utf-8"))
            _chs = _li.get("chapters") or []
            if _chs:
                from engine.manga.download_manager import _sort_chapters as _sc
                from engine.manga.download_manager import _is_volume_only as _vo
                _ch_sorted = _sc([{"id": c.get("id"), "name": c.get("name"),
                                   "group": c.get("group", "")} for c in _chs])
                _volumes = [c for c in _ch_sorted if _vo(c.get("name") or "")]
                _episodes = [c for c in _ch_sorted if not _vo(c.get("name") or "")]
                _quick = {
                    # 0.63.1：**必须同时给 comic_id**。客户端用 (source, comic_id) 去
                    # /api/manga/history 里找阅读进度；此前 quick 路径只给 "id"，
                    # 客户端读到的 comic_id 是空串 → 进度匹配失败 → 续读落点退回
                    # "第一个已下载话/第 1 话"，表现为"明明记了进度，重进却从第一话开始"
                    # （用户 2026-09-16 反馈，实测复现）。
                    "id": comic_id, "comic_id": comic_id,
                    "title": _li.get("title") or comic_id,
                    "cover": _li.get("cover") or "",
                    "source": source,
                    "source_name": (ad.name if ad else
                                    ("拷贝漫画(旧数据)" if source == "copymanga"
                                     else source)),
                    "chapters": _episodes,
                    "volumes": _volumes,
                    "downloaded": _scan_downloaded_chapters(source, comic_id),
                    "quick": True,
                }
                # R25: 少章节告警（quick 路径同样提示）
                if 0 < len(_episodes) + len(_volumes) <= 2:
                    _quick["warning"] = (
                        f"⚠ 该源仅收录 {len(_episodes) + len(_volumes)} 话，"
                        f"可能存在更完整版本（试试其他源或含“日版”的条目）")
                # B04: 后台刷新完整详情缓存（不阻塞）——经单飞注册表提交，
                # 多标签并发首访只提交一个刷新线程，失败自动入冷却
                _info_p0 = os.path.join(MANGA_DIR, "_cache", source, comic_id, "_info_full.json")
                if not os.path.exists(_info_p0):
                    _detail_refresh_async(
                        source, comic_id,
                        lambda: _refresh_detail_cache(source, comic_id, _info_p0),
                        delay=0.2)
                _quick["integrity"] = _integrity_summary(source, comic_id)
                # 0.64.0（**关键**）：这一条是"已下载 → 立即返回本地详情"的快捷路径，
                # 书库里的漫画几乎全走它。此前它**绕过** `_ensure_identity`，于是
                # 身份字段与续读解析（resume）在这条路径上全都不生效——服务端的续读
                # 修复对"已下载的漫画"等于没做（实测：详情响应里根本没有 resume）。
                return jsonify(_ensure_identity(_quick))
        except Exception:
            pass
    # R49i: 适配器不存在(如 copymanga APP 已停用)但有本地数据 → 直接返回本地结构
    if ad is None:
        _li = {}
        try:
            _li = json.load(open(_dl_info, encoding="utf-8"))
        except Exception:
            pass
        return jsonify(_ensure_identity({
            "id": comic_id, "comic_id": comic_id,
            "title": _li.get("title") or comic_id,
            "cover": _li.get("cover") or "",
            "source": source,
            "source_name": "拷贝漫画(旧数据)" if source == "copymanga" else source,
            "chapters": [],
            "volumes": [],
            "downloaded": _scan_downloaded_chapters(source, comic_id),
            "offline": True,
            "integrity": _integrity_summary(source, comic_id),
        }))
    # 详情缓存（SWR：stale-while-revalidate，对齐 Mihon 扩展"详情入库后不重拉"思路）
    # - TTL 内：直接返回缓存（详情请求频率压到最低 = 最友好防爬）
    # - TTL 后：仍先返回旧缓存 + 后台异步刷新（风控期也不阻塞用户，刷新失败不影响）
    # - chapters 为空的缓存视为 IP 风控脏数据，不命中、强制重新拉取
    _info_p = os.path.join(MANGA_DIR, "_cache", source, comic_id, "_info_full.json")
    _stale = None
    if os.path.exists(_info_p):
        try:
            _cached = json.load(open(_info_p, encoding="utf-8"))
            _cdata = _cached.get("data") or {}
            if _has_detail_content(_cdata):
                _age = time.time() - _cached.get("ts", 0)
                if _age < MANGA_DETAIL_CACHE_TTL:
                    _cdata = dict(_cdata)
                    _cdata["integrity"] = _integrity_summary(source, comic_id)
                    return jsonify(_ensure_identity(_cdata))
                # TTL 过期：先返旧缓存，后台刷新
                _stale = _cdata
        except Exception:
            pass
    if _stale:
        # B04: 后台异步 SWR 刷新（不阻塞当前响应）——经单飞注册表提交：
        # 同书并发过期访问只提交一个刷新线程，失败由注册表统一记冷却
        _detail_refresh_async(
            source, comic_id,
            lambda: _refresh_detail_cache(source, comic_id, _info_p),
            delay=0.1)
        return jsonify(_ensure_identity(_stale))
    # copymanga 风控敏感期：先走网页降级（避免 16s+ 徒劳 API 重试链）
    _fast_web = None
    if source == "copymanga" and getattr(ad, "in_cooldown", lambda: False)():
        print("[manga-detail] copymanga 风控敏感期，优先网页渠道", flush=True)
        _fast_web = _api_detail_fallback(source, comic_id, "风控敏感期")
        if _fast_web:
            return jsonify(_ensure_identity(_fast_web))
    # B04: 冷启动也走单飞注册表——同书并发冷启动只构建一次详情，
    # 跟随者等待并共享同一轮结果；失败保留冷却，不重复回源
    _data, _derr = _detail_fetch_singleflight(
        source, comic_id,
        lambda: _refresh_detail_cache(source, comic_id, _info_p, _fast_web))
    if _derr is not None:
        if isinstance(_derr, DetailUnavailable):
            # 冷却中 / 等待同书在飞构建超时：明确"暂不可用"
            return _err_json(str(_derr), 503)
        print(f"[manga-detail] 详情构建异常: {_derr}", flush=True)
        return _err_json("详情获取失败（源可能正在风控/限流）", 502)
    if _data is None:
        # 拉取与降级均失败
        return _err_json("详情获取失败（源可能正在风控/限流）", 502)
    if isinstance(_data, dict) and _data.get("gone"):
        return _err_json(_data.get("error", "漫画可能已下架/不存在"), 404)
    if _data.get("fallback"):
        return jsonify(_ensure_identity(_data))
    if not _has_detail_content(_data):
        # 真的没有任何章节（chapters 与 volumes 都空）= 风控脏数据 → 降级网页渠道。
        # 注意：**只有卷的漫画**（chapters=[] volumes=[…]）是正常数据，不能走这里
        # （旧写法因此让《巨人》每次详情多花 24 秒并被打上 fallback 标记）。
        _fallback = _fast_web or _api_detail_fallback(source, comic_id, None)
        if _fallback:
            return jsonify(_ensure_identity(_fallback))
    return jsonify(_ensure_identity(_data))


def _hedged_copymanga_info(comic_id):
    """拷贝详情对冲竞速：APP/网页通道先跑，**5 秒未出结果**则 Playwright 网页
    渠道加入竞速，首个拿到**非空章节**者胜出。

    冷启动从"串行失败链 ~29s"降为"较快一条通道的耗时"——APP 通道健康时
    不启动浏览器（Playwright 冷启动 15-20s 只在 APP 慢/挂时才付出）。
    返回 (ComicDetails|None, via_web, gone_error)。
    """
    import concurrent.futures as _cf
    app_ad = _manga_read_adapter("copymanga") or _manga_adapter("copymanga")
    web_ad = _manga_adapter("copymanga_web")

    def _run(ad):
        if ad is None:
            return ("err", None)
        try:
            return ("ok", ad.comic_info(comic_id))
        except Exception as e:
            _msg = str(e)
            if ("404" in _msg or "不存在" in _msg or "下架" in _msg):
                return ("gone", e)
            return ("err", e)

    ex = _cf.ThreadPoolExecutor(max_workers=2)
    try:
        f_app = ex.submit(_run, app_ad)
        done, _ = _cf.wait({f_app}, timeout=5.0)
        f_web = None
        if done:
            st, d = f_app.result()
            if st == "ok" and getattr(d, "chapters", None):
                return d, False, None
            if st == "gone":
                return None, False, done and f_app.result()[1]
            # APP 快速失败/空结果：立即补网页渠道（不再等慢速重试链）
        f_web = ex.submit(_run, web_ad)
        gone_err = None
        for f in _cf.as_completed({f_app, f_web}):
            try:
                st, d = f.result()
            except Exception:
                st, d = "err", None
            if st == "ok" and getattr(d, "chapters", None):
                return d, (f is f_web), None
            if st == "gone" and gone_err is None:
                gone_err = d
        return None, False, gone_err
    finally:
        ex.shutdown(wait=False)


def _refresh_detail_cache(source, comic_id, _info_p, _fast_web=None):
    """拉取详情并写入缓存。返回 _data dict 或 None（失败）。
    - 空章节结果不写缓存（IP 风控脏数据不固化）
    - 供主流程与 SWR 后台线程复用
    """
    ad = _manga_adapter(source)
    if not ad:
        return None
    # R22: copymanga 详情用快速失败实例——210 重试链 35s×N 会挂死详情页，
    # 快速失败后由下方 _api_detail_fallback 降级 copymanga_web（含快速 404）
    _hedged_web = False
    if source == "copymanga":
        # 对冲竞速：APP 与网页渠道（Playwright）取先到者（详见 _hedged_copymanga_info）
        d, _hedged_web, _gone = _hedged_copymanga_info(comic_id)
        if _gone is not None:
            print(f"[manga-detail] 漫画不存在/已下架: {str(_gone)[:200]}", flush=True)
            return {"gone": True, "error": "漫画可能已下架/不存在"}
        if d is None or not getattr(d, "chapters", None):
            return _fast_web or None
    else:
        try:
            d = ad.comic_info(comic_id)
        except Exception as e:
            # 404（漫画下架/不存在）与风控(210/502)区分提示（对齐 Kotatsu 建议）
            _msg = str(e)
            _http_code = getattr(e, "code", None) or getattr(e, "status_code", None)
            if _http_code == 404 or "404" in _msg or "不存在" in _msg or "下架" in _msg or "HTTPError" in type(e).__name__ and "404" in _msg:
                # P2-8: 原文进日志，响应只给脱敏文案
                print(f"[manga-detail] 漫画不存在/已下架: {_msg[:200]}", flush=True)
                return {"gone": True, "error": "漫画可能已下架/不存在"}
            if _fast_web is None:
                _fast_web = _api_detail_fallback(source, comic_id, e)
            return _fast_web or None
        if not d.chapters:
            if _fast_web is None:
                _fast_web = _api_detail_fallback(source, comic_id, None)
            return _fast_web or None
    _episodes, _volumes = _sort_split_chapters(d.chapters)
    _data = {
        "id": d.id, "title": d.title, "cover": d.cover,
        "sub_title": d.sub_title, "description": d.description,
        "author": d.author, "tags": d.tags,
        "upload_time": d.upload_time, "update_time": d.update_time,
        "views": d.views, "likes": d.likes, "url": d.url,
        "source": source,
        "source_name": ("拷贝漫画(网页降级)" if _hedged_web else ad.name),
        "chapters": _episodes,
        "volumes": _volumes,
        "downloaded": _scan_downloaded_chapters(source, comic_id),
        "recommend": [{"id": r.id, "title": r.title, "cover": r.cover}
                      for r in (d.recommend or [])][:8]}
    if _hedged_web:
        _data["fallback"] = True
    # R25: 少章节告警——源仅收录极少章节时提示读者找更完整版本
    # （包子《迷宫饭》简版仅 1 话，完整版是"日版"条目 112 话）
    _total_ch = len(_episodes) + len(_volumes)
    if 0 < _total_ch <= 2:
        _data["warning"] = (
            f"⚠ 该源仅收录 {_total_ch} 话，可能存在更完整版本"
            f"（试试切换其他源或搜索含“日版”的条目）")
    # 仅缓存非空详情：IP 风控期间的空结果不固化，避免锁死 self-heal
    # P3-7: atomic_write（tmp+os.replace）——直写半截 JSON 会让读者解析失败
    try:
        atomic_write(_info_p, {"ts": time.time(), "data": _data})
    except Exception:
        pass
    return _data


def _api_detail_fallback(source, comic_id, exc):
    """copymanga API 被风控时，降级到网页渠道（Playwright 渲染详情页）"""
    if source != "copymanga":
        return None
    try:
        web_ad = _manga_adapter("copymanga_web")
        if not web_ad:
            return None
        print(f"[manga-detail] copymanga API 不可用（{exc}），降级网页渠道", flush=True)
        d = web_ad.comic_info(comic_id)
        if not d.chapters:
            return None
        _episodes, _volumes = _sort_split_chapters(d.chapters)
        return {
            "id": d.id, "title": d.title, "cover": d.cover,
            "sub_title": d.sub_title, "description": d.description,
            "author": d.author, "tags": d.tags,
            "upload_time": d.upload_time, "update_time": d.update_time,
            "views": d.views, "likes": d.likes, "url": d.url,
            "source": "copymanga", "source_name": "拷贝漫画(网页降级)",
            "chapters": _episodes,
            "volumes": _volumes,
            "downloaded": _scan_downloaded_chapters("copymanga", comic_id),
            "recommend": [],
            "fallback": True}
    except Exception as e2:
        print(f"[manga-detail] 网页降级也失败: {e2}", flush=True)
        _s2 = str(e2)
        # 网页渠道识别出"漫画不存在"→ 转成 404 语义（错误 comic_id 快速失败）
        # P2-8: 原文进日志，响应只给脱敏文案
        if any(k in _s2 for k in ("漫画不存在", "404", "不存在")):
            print(f"[manga-detail] 网页渠道确认不存在: {_s2[:200]}", flush=True)
            return {"gone": True, "error": "漫画可能已下架/不存在"}
        return None


@bp.route("/api/manga/<source>/<comic_id>/chapter/<chapter_id>")
def api_manga_chapter(source, comic_id, chapter_id):

    source = _safe_seg(source, "漫画源")
    comic_id = _safe_comic_id(source, comic_id)
    chapter_id = _safe_seg(chapter_id, "章节")
    ad = _manga_read_adapter(source)  # R22: 阅读通道实例（绕过源令牌桶）
    if not ad:
        abort(404, "漫画源不存在")
    try:
        # 本地优先：已下载章节直接从磁盘读（不请求源站，秒开）
        _local = _local_chapter_images(source, comic_id, chapter_id)
        if _local is not None:
            _resp = {"images": _local, "count": len(_local),
                     "source": source, "comic_id": comic_id,
                     "chapter_id": chapter_id, "local": True}
            # 0.61.0（升级版 Venera 指南 §4 P0"旧缓存升级体验"）：旧版处理缓存必须在
            # **读者进入章节时就告诉他**"需要联网重新获取"，而不是等他看到花图/失败图。
            try:
                from engine.manga.manager import adapter_meta as _am3
                from engine.manga.downloader import _processed_marker_version as _pmv3
                _w3 = int((_am3(source) or {}).get("process_version") or 0)
                if not _w3:
                    _w3 = int(getattr(ad, "PROCESS_VERSION", 0) or 0)
                if _w3 > 0:
                    _d3 = _manga_media_root(source, comic_id, chapter_id)
                    _m3 = _pmv3(_d3, ad)
                    if _m3 is None or not str(_m3).isdigit() or int(_m3) != _w3:
                        _resp["stale_processing"] = True
                        _resp["stale_reason"] = (
                            f"本章图片是旧版处理缓存（当前算法版本 {_w3}），需联网重新获取")
            except Exception:
                pass
            return jsonify(_resp)
        imgs = _get_chapter_images(ad, source, comic_id, chapter_id)
        # 0.62.1：**阅读器一打开本章就后台预热本章**。此前只有 /urls 会预热（且只预热
        # "下一话"），而原生阅读器走的是这个端点 → 用户滚到哪一页才现取哪一页，
        # 每页一次往返（实测单页 1.5–2.5s），表现为"加载过慢"。预热是单飞 + 后台，
        # 不阻塞本响应；已下载/已缓存页会被跳过。
        try:
            _warm_chapter_images(source, comic_id, chapter_id)
        except Exception:
            pass
        return jsonify({"images": imgs, "count": len(imgs),
                        "source": source, "comic_id": comic_id,
                        "chapter_id": chapter_id})
    except Exception as e:
        return _err_response(e, 500, _img_fail_hint(e))


@bp.route("/api/manga/<source>/<comic_id>/chapter/<chapter_id>/urls")
def api_manga_chapter_urls(source, comic_id, chapter_id):

    source = _safe_seg(source, "漫画源")
    comic_id = _safe_comic_id(source, comic_id)
    chapter_id = _safe_seg(chapter_id, "章节")
    """章节图片批量接口：一次返回全部图片加载方式
    - 已下载 → 本地 API 路径（fast, 服务器直出）
    - 未下载 → 源站 CDN URL（浏览器直连，不走服务器中转）
    返回 {images: [{local: bool, url: 本地路径 或 源站URL}], count}
    """
    # R49i: 本地数据无需适配器(APP 源停用后已下载章节仍可读)
    try:
        _d0 = _manga_media_root(source, comic_id, chapter_id)
        _lf0 = []
        if os.path.isdir(_d0):
            _lf0 = [f for f in os.listdir(_d0)
                    if f.lower().endswith(( ".webp", ".jpg", ".jpeg",
                                            ".png", ".gif", ".avif"))]
        if _lf0:
            _idx0 = {int(f[:4]) for f in _lf0 if f[:4].isdigit()}
            _n0 = (max(_idx0) + 1) if _idx0 else 0
            # "完全本地"必须用**权威页数**判定，而不是"本地文件连续"：
            # 连续只说明 0..max 之间不缺页，不说明整章就这么多页。阅读缓存里
            # 只落了前 8 页（典型：加载中刷新 / 预热只跑了一半）时，旧判定直接
            # 宣布"整章 8 页"并短路返回，客户端因此永远不知道还有第 9 页——
            # 后续页面再也加载不出来，且不可自愈（实测复现用户报告的现象）。
            # 权威页数从内存/磁盘 URL 缓存读取（零源站请求，短路初衷不变）。
            _known0 = _known_chapter_pages(source, comic_id, chapter_id)
            _is_dl0 = os.path.normpath(_d0).startswith(
                os.path.normpath(MANGA_DOWNLOADS_DIR) + os.sep)
            if _known0 is None:
                # 无权威页数：只信任"已下载"目录（整章下载完成语义）
                _complete0 = _is_dl0 and _n0 > 0 and len(_idx0) == _n0
            else:
                _complete0 = _n0 > 0 and len(_idx0) == _n0 and _n0 >= _known0
            if _complete0:
                # 0.61.0（升级版 Venera 指南 §7 门槛项）：**"完全本地"短路也必须过
                # 处理版本判定**——否则旧算法写下的花图会被当成"完整章节"直接返回，
                # 用户看到的是花图，而修复后的算法根本没机会跑。
                #   · 源仍可用 → 不短路，走正常路径：客户端按源站重取，
                #     下载器在写新页前会清掉整章旧图（见 downloader.invalidate_stale_processed_dir）；
                #   · 源已下线/取不到适配器 → **保留本地阅读能力**（不锁死用户已下载内容），
                #     但如实告诉客户端"这是旧版图片缓存，需联网重新获取"。
                _ad0 = _manga_read_adapter(source)
                # 版本从**类**读（adapter_meta 不实例化）：源已下线、实例建不出来时
                # 也能判断"这是旧版缓存"，从而如实标注而不是假装完整章节。
                from engine.manga.manager import adapter_meta as _adapter_meta
                from engine.manga.downloader import is_processed_dir_current, _processed_marker_version
                _want0 = int((_adapter_meta(source) or {}).get("process_version") or 0)
                if not _want0 and _ad0 is not None:
                    # 注册表未加载（如单测里替换了阅读适配器）时退回实例属性
                    _want0 = int(getattr(_ad0, "PROCESS_VERSION", 0) or 0)
                _stale0 = False
                _reason0 = ""
                if _want0 > 0:
                    _marker0 = _processed_marker_version(_d0, _ad0) if _ad0 is not None else None
                    if _marker0 is None:
                        # 没有 marker（旧数据）→ 无法证明是当前算法
                        _cur0 = False
                    else:
                        _cur0 = int(_marker0) == _want0 if str(_marker0).isdigit() else False
                    if not _cur0:
                        _stale0 = True
                        _reason0 = (f"本章图片是旧版处理缓存（当前算法版本 {_want0}），"
                                    f"需联网重新获取")
                _fall_through = _stale0 and _ad0 is not None
                if not _fall_through:
                    out0 = [{"local": True,
                             "url": f"/api/manga/{source}/{comic_id}"
                                    f"/chapter/{chapter_id}/img/{i}"}
                            for i in range(_n0)]
                    # P1-2: 阅读期后台预取下一话（单飞 + 异常静默）
                    try:
                        _prefetch_next_chapter_images(source, comic_id, chapter_id)
                    except Exception:
                        pass
                    _resp0 = {"images": out0, "count": len(out0),
                              "source": source, "comic_id": comic_id,
                              "chapter_id": chapter_id, "local_only": True}
                    if _stale0:
                        # 源不可用时的诚实标注：本地能读，但内容是旧版处理结果
                        _resp0["stale_processing"] = True
                        _resp0["stale_reason"] = _reason0
                    return jsonify(_resp0)
                # 旧版缓存 + 源可用 → 继续往下走（回到源站重新取图并重建）
    except Exception:
        pass
    ad = _manga_read_adapter(source)  # R22: 阅读通道实例（绕过源令牌桶）
    if not ad:
        abort(404, "漫画源不存在")
    try:
        # R49c: 本地优先短路——章节已有本地图片时不再请求源站拿图列表。
        # 修复:更新下载(大量源站请求)后源站风控窗口内,打开本地旧章
        # 也被源站 images 请求拖慢/超时(前端每章都先打 /urls)。
        _d = _manga_media_root(source, comic_id, chapter_id)
        _local_files = []
        if os.path.isdir(_d):
            _local_files = [f for f in os.listdir(_d)
                            if f.lower().endswith(
                                (".webp", ".jpg", ".jpeg", ".png",
                                 ".gif", ".avif"))]
        # 0.61.0：这一层同样要过处理版本判定——否则上一层的"不短路"会在这里被
        # 又一条本地短路接住，等于白改（实测：旧版本缓存仍被 local=True 返回）。
        if _local_files:
            try:
                from engine.manga.manager import adapter_meta as _am
                from engine.manga.downloader import _processed_marker_version as _pmv
                _wv = int((_am(source) or {}).get("process_version") or 0)
                if not _wv:
                    # 注册表未加载/未注册时退回实例属性（适配器鸭子类型）
                    _wv = int(getattr(ad, "PROCESS_VERSION", 0) or 0)
                if _wv > 0:
                    _mv = _pmv(_d, ad)
                    if _mv is None or not str(_mv).isdigit() or int(_mv) != _wv:
                        print(f"[manga-read] {source}/{comic_id}/{chapter_id} "
                              f"旧版图片缓存(v={_mv} 当前={_wv})：回源重建", flush=True)
                        _local_files = []          # 走源站路径重新取图并重建整章
            except Exception:
                pass
        if _local_files:
            _idx = {int(f[:4]) for f in _local_files if f[:4].isdigit()}
            _imgs_p = os.path.join(_manga_media_root(source, comic_id),
                                   f"{chapter_id}_imgs.json")
            _urls = []
            if os.path.exists(_imgs_p):
                try:
                    _u = json.load(open(_imgs_p, encoding="utf-8"))
                    if isinstance(_u, list):
                        # R70: 旧裸数组缓存(R60 半截)不再作应有数——本地文件为准,
                        # URL 仅供缺失页补链; 新格式 {"v":2,"urls":[...]}
                        _urls = []
                    elif isinstance(_u, dict) and _u.get("v") == 2:
                        _urls = _u.get("urls") or []
                except Exception:
                    pass
            if not _urls:
                # 磁盘层没有 URL 列表 → 再看内存热层（同样零源站请求）
                _mem = _CHAPTER_IMAGES_CACHE.get((source, comic_id, chapter_id))
                if _mem and time.time() - _mem[0] < _CHAPTER_IMAGES_TTL:
                    _urls = list(_mem[1] or [])
            if not _urls:
                # 两层都没有权威 URL 列表时**绝不能用本地 max(idx)+1 当整章页数**：
                # 部分缓存（典型：加载中刷新只落了前 8 页）会被判成"整章只有 8 页"，
                # 用户看到的就是"后续未加载的页永远不再加载"（实测复现）。
                # 这里做一次有界回源（单飞 + 内存/磁盘缓存，失败即退回本地页数，
                # 保证离线也能读已下载/已缓存的部分）。
                try:
                    _ad2, _imgs_full = _manga_read_images(ad, source, comic_id,
                                                          chapter_id)
                    _urls = list(_imgs_full or [])
                    ad = _ad2 or ad
                except Exception:
                    _urls = []
            _n = max(len(_urls), (max(_idx) + 1) if _idx else 0, 1)
            out = []
            # 混淆源（jm 等：图片块倒序）缺页必须走服务器懒下载通道——直连
            # CDN 只会拿到未还原的乱序图（页面看起来是花的）
            _scr = hasattr(ad, "unscramble_image")
            for i in range(_n):
                if i in _idx:
                    out.append({"local": True,
                                "url": f"/api/manga/{source}/{comic_id}"
                                       f"/chapter/{chapter_id}/img/{i}"})
                elif _scr or not (i < len(_urls) and _urls[i]):
                    out.append({"local": False, "lazy": True,
                                "url": f"/api/manga/{source}/{comic_id}"
                                       f"/chapter/{chapter_id}/img/{i}"})
                else:
                    out.append({"local": False, "url": _urls[i]})
            if any(_e.get("lazy") for _e in out):
                # 部分本地（缺页走懒下载）→ 后台预热缺页，滚动不再等往返
                try:
                    _warm_chapter_images(source, comic_id, chapter_id)
                except Exception:
                    pass
            return jsonify({"images": out, "count": len(out),
                            "source": source, "comic_id": comic_id,
                            "chapter_id": chapter_id, "local_only": True})
        # 无本地文件 → 原逻辑（请求源站拿图列表）
        ad, imgs = _manga_read_images(ad, source, comic_id, chapter_id)
        # P1-2: 阅读期后台预取下一话（当前话列表已出，立刻异步预热下一话，
        # 用户翻页时命中内存/磁盘缓存秒回；单飞 + 异常静默）
        try:
            _prefetch_next_chapter_images(source, comic_id, chapter_id)
        except Exception:
            pass
        # 混淆源（jm 等：图片块倒序，需服务器还原）→ 一律走服务器懒下载通道，
        # 避免 App/浏览器直连 CDN 拿到未还原的乱序图片
        _scrambled = hasattr(ad, "unscramble_image")
        out = []
        for i, u in enumerate(imgs):
            if _scrambled:
                out.append({"local": False, "lazy": True,
                            "url": f"/api/manga/{source}/{comic_id}"
                                   f"/chapter/{chapter_id}/img/{i}"})
            else:
                out.append({"local": False, "url": u})
        if any(_e.get("lazy") for _e in out):
            # 混淆源（jm 等）：图片必须经服务器还原 → 启动服务器端流水线预热。
            # 用户看第一页的时间（加载/布局/阅读）里，后续页已在后台入库，
            # 滚动时命中本地磁盘（毫秒级）而不是等一次 CDN 往返。
            try:
                _warm_chapter_images(source, comic_id, chapter_id)
            except Exception:
                pass
        return jsonify({"images": out, "count": len(out),
                        "source": source, "comic_id": comic_id,
                        "chapter_id": chapter_id})
    except Exception as e:
        return _err_response(e, 500, _img_fail_hint(e))


# ══════════════════════════════════════════════════════════════
# 漫画封面（2026-09-13）
#
# 问题：书库记录的 cover 只有**源站 URL**（如 https://sa.mangafunb.fun/...），
# 本地从不保存封面文件 → 断网时浏览器取不到，卡片封面空白、书库一片无图。
# 方案：统一走服务器封面接口 /api/manga/<source>/<comic_id>/cover
#   - 本地已有 cover.<ext>（downloads 优先，其次 _cache）→ 静态直出 + 强缓存；
#   - 本地没有 → 用书库记录里的源站 URL 回源一次（SSRF 校验 + 防盗链头）
#     并落盘，之后离线可看；
#   - 书库列表打开时后台把缺失封面补齐（低并发 + 失败冷却 + 异常静默）。
# 书库响应新增 cover_view（展示用），原 cover（源站 URL）保持不变——下载/补
# 下载接口仍需要源站地址，不能被本地路径顶掉。
# ══════════════════════════════════════════════════════════════
# 封面代理单张上限（超过视为异常，不代理）
_COVER_PROXY_MAX_BYTES = 12 * 1024 * 1024

# 魔数 → MIME（与 verify.sniff_image 的返回值对应）
_IMAGE_MIME = {"jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
               "bmp": "image/bmp", "webp": "image/webp", "avif": "image/avif"}

_cover_warm_lock = threading.Lock()
_cover_warm_inflight = set()      # (source, comic_id) 补封面单飞
_cover_warm_fail = {}             # (source, comic_id) -> 失败时间（冷却）
_COVER_WARM_COOLDOWN = 900        # 失败后 15 分钟内不再重试同一封面
_COVER_WARM_MAX = 2               # 封面补齐并发（源站风控友好）


def _serve_local_cover(source, comic_id):
    """本地封面静态直出（强缓存 304）；无命中返回 None"""
    p = _local_cover_path(source, comic_id)
    if not p:
        return None
    from flask import send_from_directory
    _d, _fn = os.path.split(p)
    resp = send_from_directory(_d, _fn, conditional=True, max_age=7 * 86400)
    resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return resp


def _library_cover_url(source, comic_id):
    """取该漫画的**源站**封面地址：书库记录优先，其次详情缓存。

    只有书库记录时，搜索/浏览进来的漫画（还没加入书库）永远拿不到封面，
    详情页只好显示"无封面"——而这些漫画刚拉过详情，封面地址就在详情缓存里。
    """
    try:
        lib = _read_json(MANGA_LIBRARY_FILE, []) or []
    except Exception:
        lib = []
    if isinstance(lib, list):
        _want = {source}
        if source in ("copymanga", "copymanga_web"):
            _want = {"copymanga", "copymanga_web"}
        for x in lib:
            if not isinstance(x, dict):
                continue
            if x.get("source") in _want and str(x.get("comic_id")) == str(comic_id):
                u = (x.get("cover") or "").strip()
                if u.lower().startswith(("http://", "https://")):
                    return u
    # 详情缓存兜底（browse/search 进详情时刚写过）
    try:
        _p = os.path.join(MANGA_DIR, "_cache", source, str(comic_id), "_info_full.json")
        _c = _read_json(_p, {}) or {}
        u = ((_c.get("data") or {}).get("cover") or "").strip()
        if u.lower().startswith(("http://", "https://")):
            return u
    except Exception:
        pass
    return ""


def _fetch_cover_to_local(source, comic_id, url, timeout=20):
    """回源封面并落盘；成功返回本地路径，失败返回 None（异常静默）"""
    from engine.manga.downloader import fetch_image_checked
    _ad = _manga_read_adapter(source) or _manga_adapter(source)
    _h = {}
    if _ad is not None:
        try:
            _h = dict(_ad.image_headers(url))
        except Exception:
            _h = {}
    _h.setdefault("User-Agent",
                  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36")
    r = fetch_image_checked(url, _h, timeout=timeout)
    if r.status_code != 200 or len(r.content) < 1000:
        return None
    return _save_cover_bytes(source, comic_id, r.content)


def _warm_missing_covers(items):
    """书库列表打开时后台补本地封面（离线也能看）——低并发、单飞、失败冷却"""
    _todo = []
    _now = time.time()
    with _cover_warm_lock:
        for it in items:
            _src = (it.get("source") or "").strip()
            _cid = str(it.get("comic_id") or "").strip()
            if not _src or not _cid:
                continue
            _u = it.get("_cover_url") or ""
            if not _u:
                continue
            _k = (_src, _cid)
            if _k in _cover_warm_inflight:
                continue
            if _now - _cover_warm_fail.get(_k, 0) < _COVER_WARM_COOLDOWN:
                continue
            if _local_cover_path(_src, _cid):
                continue
            _cover_warm_inflight.add(_k)
            _todo.append((_src, _cid, _u))
        if len(_cover_warm_fail) > 500:       # 冷却表有界
            for _k in sorted(_cover_warm_fail,
                             key=lambda k: _cover_warm_fail[k])[:200]:
                _cover_warm_fail.pop(_k, None)
    if not _todo:
        return

    def _work():
        from concurrent.futures import ThreadPoolExecutor as _TPE
        def _one(t):
            _s, _c, _u = t
            try:
                _fetch_cover_to_local(_s, _c, _u)
            except Exception as e:
                with _cover_warm_lock:
                    _cover_warm_fail[(_s, _c)] = time.time()
                print(f"[manga-cover] {_s}/{_c} 封面补齐失败: "
                      f"{type(e).__name__}: {e}", flush=True)
            finally:
                with _cover_warm_lock:
                    _cover_warm_inflight.discard((_s, _c))
        with _TPE(max_workers=_COVER_WARM_MAX) as ex:
            list(ex.map(_one, _todo))

    threading.Thread(target=_work, daemon=True,
                     name="manga-cover-warm").start()


@bp.route("/api/manga/<source>/<comic_id>/cover")
def api_manga_cover(source, comic_id):
    """漫画封面：本地优先（**离线可看**），本地没有则回源一次并落盘"""
    source = _safe_seg(source, "漫画源")
    comic_id = _safe_comic_id(source, comic_id)
    _hit = _serve_local_cover(source, comic_id)
    if _hit is not None:
        return _hit
    _url = _library_cover_url(source, comic_id)
    if not _url:
        abort(404, "无封面")
    try:
        _p = _fetch_cover_to_local(source, comic_id, _url)
    except SSRFBlocked as e:
        print(f"[manga-cover] 封面地址被 SSRF 策略拒绝: {e}", flush=True)
        return _err_response(e, 502, "封面获取失败")
    except Exception as e:
        return _err_response(e, 502, "封面获取失败")
    if _p:
        _hit2 = _serve_local_cover(source, comic_id)
        if _hit2 is not None:
            return _hit2
    # 落盘失败（只读/磁盘满）也要能看：直接回内存字节
    from flask import send_file
    import io as _io
    from engine.manga.downloader import fetch_image_checked
    _ad = _manga_read_adapter(source) or _manga_adapter(source)
    _h = {}
    if _ad is not None:
        try:
            _h = dict(_ad.image_headers(_url))
        except Exception:
            _h = {}
    _r = fetch_image_checked(_url, _h, timeout=20)
    if _r.status_code != 200 or len(_r.content) < 1000:
        abort(502, "封面获取失败")
    return send_file(_io.BytesIO(_r.content), mimetype="image/webp",
                     max_age=86400)


# ── 搜索/浏览封面的**有界**代理缓存（0.68.0）────────────────────────────
#
# 背景（用户 2026-09-17 反馈"切页封面加载慢、从第 2 页回第 1 页全面卡死"）：
# 这个代理原来**每次同步回源、最长 20s、且不落任何缓存**，而引擎只有 8 个
# waitress 线程 —— 一屏封面（10+ 张）的慢回源就能把 8 个线程全占住，于是
# **所有**其它请求（搜索页自身、书库、打开章节）一起排队，表现为整个应用卡死；
# 来回翻页时封面重复回源，所以"回第 1 页"最明显。
#
# 现在：①有界磁盘缓存（命中即秒回，不再回源）；②同 URL 单飞（并发只取一次）；
# ③并发闸（最多 3 个线程在回源，其余立刻让路，界面拿占位而不是把引擎堵死）。
_COVER_PROXY_DIR = os.path.join(MANGA_CACHE_DIR, "_covers")
_COVER_PROXY_MAX_FILES = 300          # 有界：约 300 张封面（几十 MB）
_COVER_PROXY_SEM = threading.BoundedSemaphore(3)
_cover_proxy_flight = {}
_cover_proxy_lock = threading.Lock()


def _cover_proxy_key(url):
    import hashlib
    return hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()[:32]


def _cover_proxy_get(url):
    """命中缓存则返回 (bytes, 路径)；否则 (None, None)。"""
    _p = os.path.join(_COVER_PROXY_DIR, _cover_proxy_key(url) + ".img")
    try:
        if os.path.getsize(_p) > 1000:
            with open(_p, "rb") as f:
                return f.read(), _p
    except OSError:
        pass
    return None, None


def _cover_proxy_put(url, data):
    """写入缓存（原子写 + 有界淘汰）。失败只记日志，不影响本次显示。"""
    try:
        os.makedirs(_COVER_PROXY_DIR, exist_ok=True)
        _p = os.path.join(_COVER_PROXY_DIR, _cover_proxy_key(url) + ".img")
        _tmp = _p + ".tmp"
        with open(_tmp, "wb") as f:
            f.write(data)
        os.replace(_tmp, _p)
        # 有界：按 mtime 淘汰最旧的
        _files = [os.path.join(_COVER_PROXY_DIR, n)
                  for n in os.listdir(_COVER_PROXY_DIR) if n.endswith(".img")]
        if len(_files) > _COVER_PROXY_MAX_FILES:
            _files.sort(key=lambda x: os.path.getmtime(x))
            for _f in _files[:len(_files) - _COVER_PROXY_MAX_FILES]:
                try:
                    os.remove(_f)
                except OSError:
                    pass
        return _p
    except Exception as _e:
        print(f"[manga-cover] 代理封面缓存写入失败: {type(_e).__name__}: {_e}", flush=True)
        return None



def _finish_cover_flight(url, leader=True):
    """收尾：唤醒跟随者并释放并发闸（只由 leader 调用）。"""
    try:
        if leader:
            with _cover_proxy_lock:
                _ev = _cover_proxy_flight.pop(url, None)
            if _ev is not None:
                _ev.set()
    finally:
        try:
            _COVER_PROXY_SEM.release()
        except Exception:
            pass

@bp.route("/api/manga/cover")
def api_manga_cover_proxy():
    """按**源站封面地址**取图（带该源的防盗链头），给搜索/浏览结果的封面用。

    为什么必须有它：搜索/浏览出来的漫画还没进书库，`/api/manga/<源>/<id>/cover`
    只会查书库记录 → 404，界面显示"无封面"；而禁漫的图床**校验 Referer**
    （实测无 Referer 直接 403、带 Referer 200 48777B），让 App 直连源站地址也不行。
    因此由服务端带适配器自己的 image_headers 取一次，App 侧只认本机回环地址。

    0.68.0（用户反馈"切页封面加载慢、从第 2 页回第 1 页全面卡死"）：改为
    **有界磁盘缓存 + 同 URL 单飞 + 最多 3 路回源**。
    原因是旧实现每次同步回源（timeout 20s）且不缓存，而引擎只有 8 个 waitress
    线程 —— 一屏封面的慢回源就能把所有线程占住，于是搜索页自身、书库、打开章节
    全部排队，表现成整个应用卡死；来回翻页还会重复回源。

    安全与诚实：
      - 只允许 http(s) 绝对地址；SSRF 逐跳校验由 fetch_image_checked 负责
        （拒绝内网/重定向到内网），失败就明说，不做任何绕过；
      - 魔数嗅探不是图片的一律 502（不把 HTML 报错页当图片，也不写入缓存）；
      - 缓存**有界**（`_COVER_PROXY_MAX_FILES` 张，按 mtime 淘汰），不会无界增长；
      - 抢不到回源闸时**如实说明"引擎正忙、稍后重试"**，而不是把线程堵死。
    """
    from engine.manga.downloader import fetch_image_checked
    from engine.manga.verify import sniff_image
    from flask import send_file
    import io as _io

    source = _safe_seg(request.args.get("source", ""), "漫画源")
    url = (request.args.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return _err_json("url 必须是 http(s) 绝对地址", 400)
    if len(url) > 2048:
        return _err_json("url 过长", 400)

    def _send(data):
        fmt0 = sniff_image(data)
        resp = send_file(_io.BytesIO(data), mimetype=_IMAGE_MIME.get(fmt0, "image/jpeg"),
                         max_age=86400)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    # ① 缓存命中 → 秒回（翻页/回到第 1 页不再回源，也不占线程）
    _hit, _ = _cover_proxy_get(url)
    if _hit:
        return _send(_hit)

    # ③ 并发闸：最多 3 路在回源。抢不到就先等最多 2 秒（多数封面会在这段时间里
    #    轮到自己），仍抢不到立刻让路 —— 最坏等待被**封在 2 秒**，而旧实现是
    #    每个请求最长 20 秒、8 个线程一起堵（那才是"全面卡死"）。
    if not _COVER_PROXY_SEM.acquire(timeout=2.0):
        return _err_json("封面排队中（引擎正忙），稍后重试", 503)
    _leader = False
    try:
        # ② 同一 URL 单飞：并发只让一个去回源，其余等它的结果
        with _cover_proxy_lock:
            _ev = _cover_proxy_flight.get(url)
            if _ev is None:
                _ev = threading.Event()
                _cover_proxy_flight[url] = _ev
                _leader = True
        if not _leader:
            _ev.wait(timeout=8)
            _hit2, _ = _cover_proxy_get(url)
            if _hit2:
                return _send(_hit2)
            return _err_json("封面获取中，稍后重试", 503)

        ad = _manga_read_adapter(source) or _manga_adapter(source)
        if ad is None:
            return _err_json(f"漫画源不存在：{source}", 404)
        headers = {}
        try:
            headers = dict(ad.image_headers(url) or {})
        except Exception as e:
            print(f"[manga-cover] {source} image_headers 失败: {type(e).__name__}", flush=True)
        headers.setdefault("User-Agent",
                           "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36")
        try:
            r = fetch_image_checked(url, headers, timeout=20)
        except SSRFBlocked as e:
            print(f"[manga-cover] 封面地址被 SSRF 策略拒绝: {e}", flush=True)
            return _err_response(e, 502, "封面地址被安全策略拒绝")
        except Exception as e:
            return _err_response(e, 502, "封面获取失败")
        data = getattr(r, "content", b"") or b""
        status = int(getattr(r, "status_code", 0) or 0)
        if status != 200:
            return _err_json(f"封面获取失败：源站返回 HTTP {status}", 502)
        if len(data) < 1000:
            return _err_json(f"封面获取失败：数据过小（{len(data)} 字节）", 502)
        if len(data) > _COVER_PROXY_MAX_BYTES:
            return _err_json(f"封面过大（{len(data)} 字节），已拒绝代理", 502)
        if not sniff_image(data):
            return _err_json("取回的内容不是图片（已拒绝）", 502)
        _cover_proxy_put(url, data)      # 有界落盘：下次（含翻页回来）秒回
        return _send(data)
    finally:
        _finish_cover_flight(url, leader=_leader)


@bp.route("/api/manga/library")
def api_manga_library():
    """已下载漫画书库"""
    _lib_p = MANGA_LIBRARY_FILE
    lib = []
    if os.path.exists(_lib_p):
        try:
            lib = json.load(open(_lib_p, encoding="utf-8"))
        except Exception:
            pass
    # 附上本地图片数/状态；合并进行中的下载任务（实时进度）。
    # B03: 图片数/章节数取自持久化快照（下载/删除/修复增量维护 + 后台低频
    # 真实文件核对），请求路径不再 os.walk 图片目录；快照缺失时用下载记录
    # 兜底并排队后台核对（首个请求不阻塞、不扫描）。
    out = []
    seen = set()
    _warm_items = []          # 待补本地封面的条目（收集后统一后台补齐）
    for x in lib:
        x = dict(x)
        _src0, _cid0 = x.get("source", ""), x.get("comic_id", "")
        _st = _manga_stats_get(_src0, _cid0)
        if _st is None:
            _manga_stats_request(_src0, _cid0)
            nimg = int(x.get("images") or 0)
            _chapters_local = int(x.get("chapters") or 0)
        else:
            nimg = int(_st.get("images") or 0)
            _chapters_local = int(_st.get("chapters") or 0)
            # 源自动纠正：copymanga/copymanga_web 同站互查，指向本地数据多的源
            # （下载与阅读缓存可能分散在不同源目录；B03: 双源计数同出自快照，
            # 备选源在扫描时已一并统计）
            if _src0 in ("copymanga", "copymanga_web"):
                _alt_src = _st.get("alt_source")
                _alt_img = int(_st.get("alt_images") or 0)
                if _alt_src and _alt_img > nimg:
                    x["source"] = _alt_src
                    x["source_name"] = _manga_adapter_name(_alt_src)
                    nimg = _alt_img
        x["local_images"] = nimg
        # chapters：快照实扫（含图目录数）；快照缺失/本地无数据时用下载记录
        # 快照可能偏旧（下载早期建的，实测《舞冰的祈愿》话数=1 而图片=2729）→
        # **只排队重扫**，让后台把快照修对；**请求路径一律不做磁盘 IO**
        # （B03 性能不变式：重复请求不得遍历目录 —— 有专门用例守着；
        #   我第一版在这里扫磁盘，被 test_b03_library_stats 抓个正着 ✗）。
        # 自相矛盾的判据纯内存：话数 ≪ 图片数（每话至少几十张图）。
        if _st is not None and _chapters_local and nimg > 50 * _chapters_local:
            try:
                _manga_stats_request(x.get("source"), x.get("comic_id"))
            except Exception:                                    # noqa: BLE001
                pass
        x["chapters"] = _chapters_local or x.get("chapters", 0)
        # images：保留下载记录的全量图片数（本地实扫 local_images 单独展示），
        # 避免增量下载把计数覆盖成小值（1097→49）
        x["images"] = x.get("images", 0)
        # 空任务残留（0 章 0 图且非已完成记录）不展示；
        # status=done 但本地图片缺失（曾误删/缓存清理）→ 保留并标记，供一键重新下载
        if nimg == 0 and _chapters_local == 0:
            if x.get("status") == "done":
                # 快照缺失（尚未核对）时不标缺失——避免启动瞬间/核对前误报；
                # 后台首轮核对后确实无图再标记
                if _st is not None:
                    x["missing_images"] = True
            else:
                continue
        k = (x.get("source", ""), x.get("comic_id", ""))
        seen.add(k)
        # 进行中任务实时进度
        job = _manga_dl.status(_manga_dl_key(x.get("source", ""), x.get("comic_id", "")))
        if job and job.get("status") == "running":
            x["status"] = "downloading"
            x["dl_done"] = job.get("done", 0)
            x["dl_total"] = job.get("total", 0)
            x["dl_images"] = job.get("images_done", 0)
            x["dl_speed"] = job.get("speed", 0)
            x["dl_eta"] = job.get("eta", 0)
        out.append(x)
    # 进行中但尚未写入记录的（启动瞬间）
    for key, job in _manga_dl.all_tasks().items():
        if job.get("status") == "running":
            k = (job.get("source", ""), job.get("comic_id", ""))
            if k not in seen:
                out.append({"source": k[0], "comic_id": k[1], "title": job.get("title", "下载中"),
                            "cover": job.get("cover", ""), "status": "downloading",
                            "local_images": 0, "dl_done": job.get("done", 0),
                            "dl_total": job.get("total", 0),
                            "dl_images": job.get("images_done", 0),
                            "dl_speed": job.get("speed", 0), "dl_eta": job.get("eta", 0),
                            "source_name": _manga_adapter_name(job.get("source", ""))})
    # 去重 + 空标题修复：同 (comic_id) 保留数据多的记录，空标题从其他记录补
    seen_cid = {}
    for c in out:
        cid = c.get("comic_id", "")
        if not cid:
            continue
        prev = seen_cid.get(cid)
        if prev is None:
            seen_cid[cid] = c
        else:
            # 保留图片数多的；合并标题/封面
            if (c.get("local_images") or 0) > (prev.get("local_images") or 0):
                prev["source"] = c.get("source", prev.get("source"))
                prev["source_name"] = c.get("source_name", prev.get("source_name"))
                prev["local_images"] = c.get("local_images", prev.get("local_images"))
                prev["chapters"] = c.get("chapters", prev.get("chapters"))
            if not prev.get("title") and c.get("title"):
                prev["title"] = c["title"]
            if not prev.get("cover") and c.get("cover"):
                prev["cover"] = c["cover"]
    out = list(seen_cid.values())
    # R30(书库排序): 附上最近阅读时间（漫画阅读历史 ts）
    try:
        _hist = json.load(open(MANGA_HISTORY_FILE, encoding="utf-8")) or {}
    except Exception:
        _hist = {}
    for _c in out:
        _hk = _manga_dl_key(_c.get("source", ""), _c.get("comic_id", ""))
        _h = _hist.get(_hk) or {}
        _c["last_read_ts"] = _h.get("ts") or 0.0
        # 2026-09-13: 书库标注**阅读进度**（读到第几话/第几页 + 话级百分比），
        # 与"下载状态"分开显示；总话数取详情缓存（零源站请求）
        _rpos = str(_h.get("pos") or "")
        _m = re.search(r"P(\d+)", _rpos)
        _c["read_pos"] = _rpos
        _c["read_page"] = int(_m.group(1)) if _m else 0
        _c["read_title"] = _h.get("title") or ""
        # 0.64.0：书库这一行显示的"读到第N话"必须与**点进去会打开的那一话**
        # 是同一话。记录里的 idx 只是当时目录的下标，目录一变就指向别的话，
        # 所以这里也走同一个身份定位（章节 id → 章名 → 话号 → 最近 → 下标）。
        _chs = _manga_cached_chapters(_c.get("source", ""),
                                      _c.get("comic_id", ""))
        _res = _resolve_reading_position(_chs, _h) if _h else None
        if _res is not None and _res.get("index", -1) >= 0:
            _ridx = int(_res["index"])
            _c["read_idx"] = _ridx
            _c["read_exact"] = bool(_res.get("exact"))
            if _chs:
                _tot = len(_chs)
                _c["read_total"] = _tot
                # 越界不再算出 >100%（实测见过 104%）：解析出的下标必然在界内
                _c["read_ratio"] = max(0, min(100, round((_ridx + 1) / _tot * 100)))
            else:
                _tot = _manga_total_chapters(_c.get("source", ""),
                                             _c.get("comic_id", ""))
                _c["read_total"] = _tot
                _c["read_ratio"] = (max(0, min(100, round((_ridx + 1) / _tot * 100)))
                                    if _tot > 0 else 0)
            if _res.get("note"):
                _c["read_note"] = _res["note"]
            # 记录里的章名（用户上次读到的话）；与解析结果不一致时界面要说明
            _c["read_label"] = _res.get("record_label") or ""
            _c["read_matched_label"] = _res.get("matched_label") or ""
        else:
            _ridx = int(_h.get("idx") or 0) if _h else 0
            _c["read_idx"] = _ridx
            _c["read_exact"] = False
            _c["read_label"] = _res.get("record_label") if _res else ""
            _c["read_matched_label"] = ""
            if _res is not None and _res.get("note"):
                _c["read_note"] = _res["note"]
            if _ridx or _rpos:
                _tot = _manga_total_chapters(_c.get("source", ""),
                                             _c.get("comic_id", ""))
                _c["read_total"] = _tot
                _c["read_ratio"] = 0
            else:
                _c["read_total"] = 0
                _c["read_ratio"] = 0
        # 封面展示地址：统一走服务器封面接口（本地优先 → 断网也能显示）。
        # 原 cover（源站 URL）保持不变——"重新下载/补下载"接口还要用它。
        _cs, _cc = _c.get("source", ""), str(_c.get("comic_id", ""))
        _remote = (_c.get("cover") or "").strip()
        _has_local = bool(_local_cover_path(_cs, _cc)) if (_cs and _cc) else False
        _c["cover_view"] = (f"/api/manga/{_cs}/{_cc}/cover"
                            if (_cs and _cc and (_has_local or _remote)) else "")
        if _remote and not _has_local:
            # 本地还没有封面 → 后台补齐（打开一次书库，之后离线也有封面）
            _warm_items.append({"source": _cs, "comic_id": _cc,
                                "_cover_url": _remote})
    _warm_items = _warm_items[:40]        # 有界：一次最多补 40 张
    try:
        if _warm_items:
            _warm_missing_covers(_warm_items)
    except Exception:
        pass
    # B03: 轻量快照附全局 revision——客户端据此判断无变化时跳过重绘
    return jsonify({"comics": out, "rev": manga_library_revision()})


@bp.route("/api/manga/library/<source>/<comic_id>", methods=["DELETE"])
def api_manga_library_delete(source, comic_id):

    source = _safe_seg(source, "漫画源")
    comic_id = _safe_comic_id(source, comic_id)
    _lib_p = MANGA_LIBRARY_FILE
    # R29(技术评审5.3): 与下载管理器的书库写共用同一把锁 + 原子替换，
    # 防止删除与任务完成并发时丢更新
    with _manga_dl._lock:
        lib = []
        if os.path.exists(_lib_p):
            try:
                lib = json.load(open(_lib_p, encoding="utf-8"))
            except Exception:
                lib = []
        lib = [x for x in lib if not (x.get("source") == source and x.get("comic_id") == comic_id)]
        # R47: 原子写收敛到 app_utils.atomic_write 单一实现
        atomic_write(_lib_p, lib)
    import shutil
    # 清所有相关源缓存（copymanga / copymanga_web 同站互查）+ 历史
    _removed = []
    for _s in (source, "copymanga", "copymanga_web"):
        _base = os.path.join(MANGA_CACHE_DIR, _s, comic_id)
        if os.path.isdir(_base):
            shutil.rmtree(_base, ignore_errors=True)
            _removed.append(_s)
        _imgs = os.path.join(MANGA_CACHE_DIR, _s, f"{comic_id}_imgs.json")
        if os.path.exists(_imgs):
            os.remove(_imgs)
    # 历史记录清理（P3-7: atomic_write 原子写，防止半截 JSON 丢全部历史）
    _hist_p = MANGA_HISTORY_FILE
    try:
        _hist = json.load(open(_hist_p, encoding="utf-8"))
        # 历史是按 "<source>:<comic_id>" 作**键**存的（值里只有 idx/pos/title/ts）。
        # 原实现按值的 v["source"]/v["comic_id"] 过滤——这两个字段从来不存在，
        # 于是"从书库移除"从来没清掉过阅读历史，残留位置会在重新加入时复活
        # （实测：删除后历史条数仍为 1）。改为按键过滤，并覆盖同站别名源。
        _keys = {f"{s}:{comic_id}" for s in (source, "copymanga", "copymanga_web")}
        _hist = {k: v for k, v in _hist.items() if k not in _keys}
        atomic_write(_hist_p, _hist)
    except Exception:
        pass
    # 任务清理
    _manga_dl.delete(_manga_dl_key(source, comic_id))
    # B03: 书库统计快照同步摘除（含同站互查的备选源条目）
    for _s in (source, "copymanga", "copymanga_web"):
        _manga_stats_note_change(_s, comic_id, removed=True)
    # 2026-09-15：可选**连已下载文件一起删**。
    # 默认保持原行为（只移除记录，文件留在磁盘）——桌面网页端一直是这样，不能悄悄改。
    # 但手机端必须能真正释放空间：实测"从书库移除"后 14MB 图片仍在，而 App 里没有
    # 别的入口能删掉它们（方向基线 §8.D："取消任务和删除已下载内容是不同操作"）。
    _data = request.get_json(silent=True) or {}
    _purge = bool(_data.get("files")) or str(request.args.get("files", "")).lower() in ("1", "true", "yes")
    _freed = 0
    _purged = []
    if _purge:
        import shutil as _sh
        for _s in (source, "copymanga", "copymanga_web"):
            _dl_dir = os.path.join(MANGA_DOWNLOADS_DIR, _s, comic_id)
            if os.path.isdir(_dl_dir):
                for _root, _dirs, _files in os.walk(_dl_dir):
                    for _f in _files:
                        try:
                            _freed += os.path.getsize(os.path.join(_root, _f))
                        except OSError:
                            pass
                _sh.rmtree(_dl_dir, ignore_errors=True)
                _purged.append(_s)
    return jsonify({"ok": True, "removed": _removed,
                    "purged": _purged, "freed_bytes": _freed})


@bp.route("/api/manga/<source>/<comic_id>/download", methods=["POST"])
def api_manga_download_start(source, comic_id):

    source = _safe_seg(source, "漫画源")
    comic_id = _safe_comic_id(source, comic_id)
    data = request.get_json(silent=True) or {}
    title = data.get("title", comic_id)
    chapters = data.get("chapters") or []  # 章节选择下载（可选）
    # R68: 启动即带封面——避免书库/任务 cover 为空导致封面缺失
    key, status = _manga_dl.start(source, comic_id, title, chapters=chapters,
                                  cover=data.get("cover", ""))
    return jsonify({"ok": True, "status": status})


@bp.route("/api/manga/download/status")
def api_manga_download_status():
    source = _safe_seg(request.args.get("source", ""), "漫画源")
    comic_id = _safe_comic_id(source, request.args.get("cid", ""))
    key = _manga_dl_key(source, comic_id)
    return jsonify(_manga_dl.status(key))


@bp.route("/api/manga/download/pause", methods=["POST"])
def api_manga_download_pause():
    source = _safe_seg(request.args.get("source", ""), "漫画源")
    comic_id = _safe_comic_id(source, request.args.get("cid", ""))
    _manga_dl.pause(_manga_dl_key(source, comic_id))
    return jsonify({"ok": True})


@bp.route("/api/manga/download/pause-all", methods=["POST"])
def api_manga_download_pause_all():
    """批量暂停所有进行中的漫画下载"""
    n = 0
    for key in _manga_dl.running_keys():
        _manga_dl.pause(key)
        n += 1
    return jsonify({"ok": True, "paused": n})


@bp.route("/api/manga/download/resume", methods=["POST"])
def api_manga_download_resume():
    source = _safe_seg(request.args.get("source", ""), "漫画源")
    comic_id = _safe_comic_id(source, request.args.get("cid", ""))
    _manga_dl.resume(_manga_dl_key(source, comic_id))
    return jsonify({"ok": True})


@bp.route("/api/manga/download/resume-all", methods=["POST"])
def api_manga_download_resume_all():
    """批量启动所有暂停/停止的漫画下载"""
    n = 0
    for key in _manga_dl.paused_keys():
        _manga_dl.resume(key)
        n += 1
    return jsonify({"ok": True, "resumed": n})


@bp.route("/api/manga/<source>/<comic_id>/check-update", methods=["POST"])
def api_manga_check_update(source, comic_id):

    source = _safe_seg(source, "漫画源")
    comic_id = _safe_comic_id(source, comic_id)
    """检查更新（单部）：对比源站全部章节 vs 本地已下载章节
    R54: force_refresh=True——与一键一致, 真实核对源站最新章节
    (此前默认走本地快照秒回, 源站更新了也发现不了, 名不副实)"""
    return jsonify(_manga_check_one(source, comic_id, force_refresh=True))


@bp.route("/api/manga/library/check-updates", methods=["POST"])
def api_manga_library_check_updates():
    """一键检查书库更新(异步启动): 逐部结果经 /status 轮询。
    跳过下载中/排队中的(其缺失是任务未下完, 非更新)。"""
    with _manga_check_lock:
        if _manga_check_state["running"]:
            return jsonify({"ok": False, "error": "检查已在进行中"}), 409
        # P1-3: 判定与登记(running=True)同锁一次完成——此前 running=True 在
        # 构建 items 之后才写，两个并发请求都能越过 running==False 判定、
        # 各起一个 worker 双写 results。
        _manga_check_state.update(running=True, total=0, started_at=0.0)
        _manga_check_state["results"] = []
        _manga_check_state["finished_at"] = 0.0
    # items 准备（json.load / _manga_dl.status / 书库类型）与 Thread.start 同处
    # 一个 try：任一步异常都必须复位 running。此前 items 准备在 try 之外，
    # _manga_dl.status 抛错或 MANGA_LIBRARY_FILE 为非法类型（非 list）会让
    # running 永久为 True，一键检查从此恒定 409。
    try:
        items = []
        try:
            lib = json.load(open(MANGA_LIBRARY_FILE, encoding="utf-8"))
        except Exception:
            lib = []
        if not isinstance(lib, list):
            lib = []
        for x in lib:
            if not isinstance(x, dict) or x.get("status") != "done":
                continue
            src = x.get("source", "")
            cid = x.get("comic_id", "")
            if not _SAFE_SEG_RE.match(cid or "") or ".." in (cid or ""):
                continue
            if _manga_dl.status(_manga_dl_key(src, cid)).get("status") in \
                    ("running", "queued", "paused"):
                continue
            items.append((src, cid, x.get("title") or cid))
        if not items:
            with _manga_check_lock:
                _manga_check_state.update(running=False, total=0,
                                          finished_at=time.time())
            return jsonify({"ok": True, "total": 0, "results": []})
        with _manga_check_lock:
            _manga_check_state.update(total=len(items), started_at=time.time())
        threading.Thread(target=_manga_check_worker, args=(items,),
                         daemon=True).start()
    except Exception as e:
        # items 准备 / 线程启动失败：必须复位 running，否则一键检查从此恒定 409
        with _manga_check_lock:
            _manga_check_state["running"] = False
        return _err_response(e, 500, "检查任务启动失败")
    return jsonify({"started": True, "total": len(items)})


@bp.route("/api/manga/library/check-updates/status")
def api_manga_library_check_updates_status():
    """一键检查书库更新进度/结果(轮询)"""
    with _manga_check_lock:
        st = dict(_manga_check_state)
    results = st.pop("results", [])
    st["done"] = len(results)
    st["results"] = results
    return jsonify(st)


@bp.route("/api/manga/check-updates-download", methods=["POST"])
def api_manga_check_updates_download():
    """一键补充下载：下载最近一次批量检查发现有更新的漫画缺失章节"""
    started = 0
    _now = time.time()
    # 与 _manga_check_worker 的增删同锁：避免读到半构造条目
    with _manga_check_lock:
        _snap = list(_mcache.items())
    # P1-4: _mcache 键为 (source, comic_id)，与 _manga_check_worker 写入侧一致
    for (_src, _cid), _info in _snap:
        if _now - (_info.get("ts") or 0) > 600:
            continue
        _miss = _info.get("missing") or []
        if not _miss:
            continue
        if not _info.get("source"):
            continue
        _manga_dl.start(_info["source"], _cid, _info.get("title") or _cid,
                        chapters=_miss)
        started += 1
    return jsonify({"ok": True, "started": started})


@bp.route("/api/manga/<source>/<comic_id>/download-new", methods=["POST"])
def api_manga_download_new(source, comic_id):

    source = _safe_seg(source, "漫画源")
    comic_id = _safe_comic_id(source, comic_id)
    """增量下载新章节（配合 check-update）"""
    data = request.get_json(silent=True) or {}
    title = data.get("title", comic_id)
    new_chapters = data.get("new_chapters") or []
    if not new_chapters:
        return _err_json("无新章节")
    key, status = _manga_dl.start(source, comic_id, title, chapters=new_chapters,
                                  cover=data.get("cover", ""))
    return jsonify({"ok": True, "status": status})


@bp.route("/api/manga/history", methods=["GET"])
def api_manga_history():
    """阅读历史（最近 50 条，按更新时间倒序）"""
    hist = _read_json(MANGA_HISTORY_FILE, {})
    items = []
    for k, v in hist.items():
        src, cid = k.split(":", 1) if ":" in k else ("", k)
        items.append({"source": src, "comic_id": cid, **v})
    items.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return jsonify({"history": items[:50]})


@bp.route("/api/manga/history", methods=["POST"])
def api_manga_history_save():
    data = request.get_json(silent=True) or {}
    source = data.get("source", "")
    comic_id = data.get("comic_id", "")
    idx = data.get("idx", 0)
    pos = data.get("pos", "")
    title = data.get("title", comic_id)
    # 0.64.0：**章节身份**必须一起记。只记 idx 时，目录一变（加更/删章/插番外）
    # 下标就指向别的一话，表现为"书库显示读到第47话，点进去却是第1话"。
    chapter_id = str(data.get("chapter_id") or "")[:200]
    chapter_label = str(data.get("chapter_label") or "")[:200]
    if not source or not comic_id:
        return _err_json("参数不完整")
    # 不归一化会让 jm123 与 123 各存一条进度，表现为"续读位置丢失"
    comic_id = _norm_comic_id(source, comic_id)
    key = f"{source}:{comic_id}"

    def _merge(hist):
        hist = hist or {}
        _prev = (hist.get(key) or {}) if isinstance(hist, dict) else {}
        hist[key] = {"idx": idx, "pos": pos, "title": title,
                     # 章名兜底：客户端没给 chapter_label 时，从 pos 里取
                     "chapter_id": chapter_id,
                     "chapter_label": chapter_label or _pos_chapter_label(pos),
                     # 旧字段保留（网页端与历史文件向前兼容）
                     "ts": time.time()}
        if not chapter_id and _prev.get("chapter_id"):
            hist[key]["chapter_id"] = _prev["chapter_id"]
        # 限 200 条
        if len(hist) > 200:
            for k in sorted(hist, key=lambda x: hist[x].get("ts", 0))[:50]:
                hist.pop(k, None)
        return hist

    # 读-改-写在同一临界区（A05）：并发保存不同漫画时后写覆盖先写；
    # 写盘失败必须显式失败，不得返回 ok:true 让前端以为已同步
    try:
        update_json(MANGA_HISTORY_FILE, _merge, {})
    except Exception as e:
        # A05 脱敏（P2-8，同 _err_response 模式）：OSError 等原始异常消息含
        # 服务器绝对路径，只记服务端日志；客户端拿通用文案。
        # 响应形状保持 {"ok": False, "error": ...}（前端按 d.ok !== true 判失败）
        import traceback
        traceback.print_exc()
        print(f"[error] 进度保存失败: {type(e).__name__}: {e}", flush=True)
        return jsonify({"ok": False, "error": "进度保存失败，请稍后重试"}), 500
    return jsonify({"ok": True})


@bp.route("/api/manga/favorites", methods=["GET"])
def api_manga_favorites():
    fav = _read_json(MANGA_FAV_FILE, {})
    items = []
    for k, v in fav.items():
        src, cid = k.split(":", 1) if ":" in k else ("", k)
        items.append({"source": src, "comic_id": cid, **v})
    return jsonify({"favorites": items})


@bp.route("/api/manga/favorites", methods=["POST"])
def api_manga_favorites_add():
    data = request.get_json(silent=True) or {}
    source = data.get("source", "")
    comic_id = data.get("comic_id", "")
    title = data.get("title", comic_id)
    cover = data.get("cover", "")
    if not source or not comic_id:
        return _err_json("参数不完整")
    # 与 DELETE 分支保持同一键：不归一化会导致 jm123 收藏后按 123 删不掉，
    # 且同一部漫画出现两条收藏记录
    comic_id = _norm_comic_id(source, comic_id)
    _k = f"{source}:{comic_id}"
    _v = {"title": title, "cover": cover, "ts": time.time()}

    def _add(d):
        d = d if isinstance(d, dict) else {}
        d[_k] = _v
        return d
    # 读-改-写在同一临界区（_read_json + _write_json 分别加锁会让并发
    # 收藏/取消互相覆盖）；写失败按脱敏约定返回 500
    try:
        update_json(MANGA_FAV_FILE, _add, {})
    except Exception as e:
        return _err_response(e, 500, "收藏保存失败")
    return jsonify({"ok": True})


@bp.route("/api/manga/favorites/<source>/<comic_id>", methods=["DELETE"])
def api_manga_favorites_del(source, comic_id):

    source = _safe_seg(source, "漫画源")
    comic_id = _safe_comic_id(source, comic_id)
    _k = f"{source}:{comic_id}"

    def _del(d):
        d = d if isinstance(d, dict) else {}
        d.pop(_k, None)
        return d
    try:
        update_json(MANGA_FAV_FILE, _del, {})
    except Exception as e:
        return _err_response(e, 500, "收藏保存失败")
    return jsonify({"ok": True})


@bp.route("/api/manga/<source>/<comic_id>/zip")
def api_manga_zip(source, comic_id):

    source = _safe_seg(source, "漫画源")
    comic_id = _safe_comic_id(source, comic_id)
    """打包已下载章节为 zip（按章节分目录）"""
    import zipfile
    base = _manga_media_root(source, comic_id)
    if not os.path.isdir(base):
        abort(404, "未下载")
    # P3-8: 不再 BytesIO 全量入内存（整部漫画数百 MB 内存尖峰）——
    # 打包到临时文件后 send_file 流式发送；call_on_close 在 WSGI 响应
    # 迭代完毕后触发，此时清理临时文件（提前删会让响应体读不到数据）。
    import tempfile as _tf
    _fd, _tmp = _tf.mkstemp(prefix="manga_zip_", suffix=".zip")
    os.close(_fd)
    try:
        with zipfile.ZipFile(_tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for root, _dirs, files in os.walk(base):
                for fn in sorted(files):
                    if fn.endswith((".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif")):
                        p = os.path.join(root, fn)
                        rel = os.path.relpath(p, base)
                        zf.write(p, rel)
    except Exception as _e:
        try:
            os.remove(_tmp)
        except OSError:
            pass
        return _err_response(_e, 500, "打包失败")
    from flask import send_file
    _zf = open(_tmp, "rb")
    resp = send_file(_zf, mimetype="application/zip",
                     download_name=f"{comic_id}.zip", as_attachment=True)

    @resp.call_on_close
    def _cleanup_zip():
        try:
            _zf.close()
        except OSError:
            pass
        try:
            os.remove(_tmp)
        except OSError:
            pass
    return resp


# P1-4: /img/<idx> 阅读通道加固——下载器实例复用 + in-flight 单飞去重
_img_dl_lock = threading.Lock()


_img_dl_cache = {}   # (source, comic_id) -> ImageDownloader（实例级并发信号量跨请求生效）


_IMG_DL_CACHE_MAX = 20  # 复用实例 LRU 上限（防实例泄漏）


_img_fetching = {}   # (source, comic_id, chapter_id, idx) -> _ImgFlight（单飞）


_img_fetching_lock = threading.Lock()


_IMG_FETCH_MAX = 200     # 在飞图片单飞上限（超限淘汰最旧，防挂死泄漏）


_IMG_FETCH_STALE = 180   # 在飞条目挂死上限（秒）


_IMG_FAIL_COOLDOWN = 5   # B05: 图片失败秒级冷却——同轮等待者共享失败 +
                         # 防瞬时重试风暴；冷却后用户刷新即可重试


_IMG_FETCH_WAIT_TIMEOUT = 30  # 跟随者等待同图结果上限（秒）
# 浏览器同域并发连接有限（HTTP/1.1 约 6 条）：跟随者长时间占住连接，会让
# 排在其后的图片请求「永不上屏」——用户看到的就是"后续页面不再加载"。
# 上限取 30s：正常单图几秒内出结果，超时即明确 503，交给客户端按同地址
# 缓存击穿重试（早期实现一律走 /proxy，对 jm 的相对地址必然失败）。



_img_fail_ts = {}        # (source, comic_id, chapter_id, idx) -> 失败时间戳


class _ImgFlight:
    """图片单飞句柄：done 事件 + 同轮共享的成功标志。
    B05: leader 的成功/失败对所有等待者可见——跟随者不再在 leader 失败或
    超时后绕过单飞自行回源（杜绝第二个同图生产者）。"""
    __slots__ = ("done", "ok", "ts")

    def __init__(self):
        self.done = threading.Event()
        self.ok = False
        self.ts = time.time()


def _read_downloader(ad, source, comic_id):
    """阅读通道 ImageDownloader 按 (source, comic_id) 缓存复用（LRU 上限 20）。
    旧实现每请求 new 实例——实例级并发信号量/节流随实例销毁，形同虚设。
    阅读通道：不节流（阅读优先，下载任务才节流防风控）+ 高并发 8"""
    from engine.manga.downloader import ImageDownloader
    key = (source, comic_id)
    with _img_dl_lock:
        dl = _img_dl_cache.get(key)
        if dl is None:
            dl = ImageDownloader(
                ad, os.path.join(MANGA_CACHE_DIR, source, comic_id),
                min_interval=0.0, concurrency=8)
            _img_dl_cache[key] = dl
            # 超限淘汰最旧（dict 保插入序）
            while len(_img_dl_cache) > _IMG_DL_CACHE_MAX:
                _img_dl_cache.pop(next(iter(_img_dl_cache)))
    return dl


def _sniff_image_mime(path):
    """按文件头判断真实图片类型（0.65.0）。

    为什么需要：混淆源的兜底路径会把**转码后的字节**写进以源扩展名命名的文件
    （历史遗留：先是 PNG 字节叫 `.webp`，0.65.0 起是 JPEG 字节）。照扩展名回
    Content-Type 会与实际内容不符——浏览器/客户端只能靠嗅探侥幸读对。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except Exception:
        return ""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:3] == b"GIF":
        return "image/gif"
    if head[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return ""


def _serve_local_image(_d, idx, adapter=None):
    """本地已下载图片静态直出（强缓存 304）；无命中返回 None。

    带处理版本的源必须先确认章节缓存与当前算法一致，避免修复块还原后仍
    直接发送旧版花图。适配器不可用时保留本地阅读能力，不能因源下线而锁死
    用户已下载内容。
    """
    if adapter is not None:
        from engine.manga.downloader import is_processed_dir_current
        if not is_processed_dir_current(_d, adapter):
            return None
    for _ext in (".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif"):
        _cp = os.path.join(_d, f"{idx:04d}{_ext}")
        if os.path.exists(_cp) and os.path.getsize(_cp) > 1000:
            from flask import send_from_directory
            # 0.65.0：**按内容嗅探**而不是按扩展名给 Content-Type。
            # 兜底路径曾经把 PNG 字节写进 `.webp` 文件名，现在是 JPEG 字节写进
            # `.webp`；照扩展名回 image/webp 是错的（客户端靠嗅探侥幸能读）。
            _mime = _sniff_image_mime(_cp) or ("image/" + _ext.lstrip("."))
            resp = send_from_directory(
                _d, f"{idx:04d}{_ext}",
                mimetype=_mime,
                conditional=True, max_age=7 * 86400)
            # 本地已下载文件内容不变 → immutable 长期缓存，再次阅读零请求
            resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
            return resp
    return None


# ══════════════════════════════════════════════════════════════
# 阅读预热带（2026-09-10 性能优化）
#
# 背景：jm 等"混淆源"的图片必须经服务器还原，浏览器只能逐张打 /img；而源站
# CDN 单 IP 带宽/延迟受限（实测每图数百毫秒到数秒）。逐张按需加载时，用户
# 每次滚动都要等一个完整往返，滚动手感就是"卡在等图"。
# 方案：**服务器端流水线预热**——/urls 一返回，后台就按顺序把该话图片
# 抓取+还原进阅读缓存；用户滚动时命中的是本地磁盘（毫秒级）。
# 约束：
#   - 低并发（默认 2），给用户当前可见页留出下载器槽位，不抢带宽
#   - 单飞（同话只跑一个预热线程）
#   - **代际守卫**：同一部漫画切到新话时，旧话的预热立即退出（不浪费
#     受限带宽去抓用户已经离开的章节）
#   - 跳过已缓存页；异常静默（预热失败不影响正常按需加载）
# ══════════════════════════════════════════════════════════════
_warm_lock = threading.Lock()
_warm_inflight = set()          # (source, comic_id, chapter_id) 预热单飞
_warm_gen = {}                  # (source, comic_id) -> 用户正在读的话（代际）
# 预热并发：实测（2026-09-16）禁漫 CDN 对并行是**真的并行**——裸 requests 3 线程取 3 张
# 总耗时 1.88s（≈单张耗时），而我们的下载器串行取 2 张要 3.41s。预热只有 2 并发时，
# 一话 40 页要 ~80s 才填满，用户滚动到哪都还要等 2s（"禁漫加载过慢"的直接来源）。
# 提高到 4：仍远低于阅读通道按需取图的 8 并发，给当前可见页留出余量。
_WARM_CONCURRENCY = 8

# 两段式预热（0.68.0）：先以低并发把"用户马上要看的前几页"拿到，再整段并发补齐。
# 依据：用户反馈"基本上是全图片加载完后才能开始阅读，而不是逐页加载"——
# 8 路并发抢带宽/源站连接时，用户正在等的第 1 页反而排在后面。
_WARM_LEAD_PAGES = 3
_WARM_LEAD_CONCURRENCY = 2
_WARM_ABORT_POLL = 4            # 每取若干页检查一次代际（切话即退出）
_NEXT_WARM_PAGES = 5            # "下一话"预热的页数（首屏够用，不抢带宽）


def _warm_chapter_images(source, comic_id, chapter_id, limit=None,
                         concurrency=_WARM_CONCURRENCY, guard_chapter=None):
    """后台预热一话图片到阅读缓存（同话单飞 + 切话即弃）。

    guard_chapter=None → **主预热**：把 (source, comic_id) 的代际设为本章
      （用户当前在读的话），并在整话预热完成后**接力**预热下一话前几页；
    guard_chapter=<正在读的话> → **次预热（下一话）**：不覆盖代际，仅当
      用户仍停在该话时继续，否则立即放弃。
    limit=None 表示整话。返回 True 表示已启动或在预热中。
    """
    # 关预热的三条途径（按优先级）：
    #   · WR_DISABLE_MANGA_WARM=1 —— 只关漫画预热（大流量计费/源站限流紧张时用）
    #   · WR_BG_PREWARM=0 / WR_DISABLE_BACKGROUND=1 —— 统一的"后台工作"总开关
    # 旧实现只看第一条，于是在 `WR_DISABLE_BACKGROUND=1` 的测试环境里**照样起后台
    # 预热线程**：它们会往用例的临时数据目录写缓存，留下 .txn.lock 之类中间文件，
    # 让"无残留"断言随机失败（实测：test_r39_repair_overwrite 全量跑红、单独跑绿）。
    if os.environ.get("WR_DISABLE_MANGA_WARM") == "1":
        return False
    try:
        from server.runtime import worker_enabled
        if not worker_enabled("prewarm"):
            return False
    except Exception:
        pass
    _key = (source, comic_id, chapter_id)
    _guard = chapter_id if guard_chapter is None else guard_chapter
    with _warm_lock:
        if _key in _warm_inflight:
            return True
        _warm_inflight.add(_key)
        if guard_chapter is None:
            _warm_gen[(source, comic_id)] = chapter_id   # 仅主预热设代际
            if len(_warm_gen) > 200:
                for _k in list(_warm_gen)[:-100]:
                    _warm_gen.pop(_k, None)

    def _work():
        from concurrent.futures import ThreadPoolExecutor, as_completed
        try:
            ad = _manga_read_adapter(source)
            if not ad:
                return
            ad, imgs = _manga_read_images(ad, source, comic_id, chapter_id)
            if not imgs:
                return
            dl = _read_downloader(ad, source, comic_id)
            _dir = os.path.join(MANGA_CACHE_DIR, source, comic_id, chapter_id)
            _n = len(imgs) if limit is None else min(limit, len(imgs))
            _todo = []
            for i in range(_n):
                # 代际守卫：用户已切到别的话 → 立即放弃本话剩余预热
                if _warm_gen.get((source, comic_id)) != _guard:
                    return
                if _page_cached(_dir, i):
                    continue
                _todo.append(i)
            # 本话已全缓存（重读旧章/预热已完成）时不做无用回源，但**不能提前
            # return**——接力下一话的逻辑就在下面，提前返回会让"读完这章再翻下
            # 一章"永远没有预热（实测下一话首图退回一次完整 CDN 往返）。
            # **两段式预热**（0.68.0，用户反馈"要等整话加载完才能开始读"）：
            # 旧实现一上来就 8 路并发抓整话，把带宽与源站连接全占住，用户正在等的
            # 第 1 页反而要排队 —— 体感就是"读不了，得等全部加载完"。
            # 现在先在**低并发**下按顺序把"马上要读的前几页"拿到，再用整段并发补齐。
            def _run(_items, _workers):
                if not _items:
                    return
                _done = 0
                with ThreadPoolExecutor(max_workers=max(1, _workers)) as ex:
                    futs = [ex.submit(_warm_one, dl, imgs, source, comic_id,
                                      chapter_id, i) for i in _items]
                    for f in as_completed(futs):
                        _done += 1
                        if _done % _WARM_ABORT_POLL == 0 and \
                                _warm_gen.get((source, comic_id)) != _guard:
                            for _f in futs:
                                _f.cancel()
                            return

            if _todo:
                _lead = [i for i in _todo if i < _WARM_LEAD_PAGES]
                _rest = [i for i in _todo if i >= _WARM_LEAD_PAGES]
                _run(_lead, _WARM_LEAD_CONCURRENCY)     # 先保"马上要读的"
                if _warm_gen.get((source, comic_id)) == _guard:
                    _run(_rest, concurrency)            # 再补齐整话
            # 主预热完成 → 接力预热下一话前几页（低并发、跟随代际守卫），
            # 用户切到下一话时首屏已就绪；若已切走则由守卫立即放弃
            if guard_chapter is None:
                _warm_next_after(source, comic_id, chapter_id)
        except Exception as e:
            print(f"[manga-warm] {source}/{comic_id}/{chapter_id} 预热失败: "
                  f"{type(e).__name__}: {e}", flush=True)
        finally:
            with _warm_lock:
                _warm_inflight.discard(_key)

    threading.Thread(target=_work, daemon=True,
                     name=f"manga-warm-{source}").start()
    return True


def _warm_next_after(source, comic_id, chapter_id):
    """主预热完成后接力：下一话前 _NEXT_WARM_PAGES 页（异常静默）"""
    try:
        _info_p = os.path.join(MANGA_CACHE_DIR, source, comic_id,
                               "_info_full.json")
        if not os.path.exists(_info_p):
            return
        with open(_info_p, encoding="utf-8") as f:
            _chs = (json.load(f).get("data") or {}).get("chapters") or []
        _ids = [_c.get("id") for _c in _chs if _c.get("id")]
        if chapter_id not in _ids:
            return
        _ni = _ids.index(chapter_id) + 1
        if _ni >= len(_ids):
            return
        _next = _ids[_ni]
        if _manga_media_root(source, comic_id, _next) and _page_cached(
                _manga_media_root(source, comic_id, _next), 0):
            return                       # 下一话已下载/已缓存 → 无需预热
        _warm_chapter_images(source, comic_id, _next,
                             limit=_NEXT_WARM_PAGES, guard_chapter=chapter_id)
    except Exception:
        pass


def _page_cached(_dir, idx):
    """该页是否已在阅读缓存/下载目录（>1000B 视为可用，同 /img 判定）"""
    for _ext in (".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif"):
        _cp = os.path.join(_dir, f"{idx:04d}{_ext}")
        try:
            if os.path.getsize(_cp) > 1000:
                return True
        except OSError:
            continue
    return False


def _warm_one(dl, imgs, source, comic_id, chapter_id, i):
    try:
        dl.get(imgs[i], comic_id, chapter_id, i)
        return True
    except Exception:
        return False


def _serve_remote_image(ad, source, comic_id, chapter_id, idx):
    """未下载章节的远程读图（URL 缓存 + 复用下载器，其磁盘缓存优先）。

    缓存命中：改为**磁盘静态直出**（send_from_directory + immutable）——
    避免把整张图读进内存再包 BytesIO 发送，并让浏览器后续访问走 304/本地
    缓存；仅在文件缺失的边缘情形回落内存路径。"""
    from flask import send_file
    import io as _io
    # 未下载：走源站（带 URL 缓存，避免重复请求；copymanga 210 自动降级 web 通道）
    ad, imgs = _manga_read_images(ad, source, comic_id, chapter_id)
    if idx < 0 or idx >= len(imgs):
        abort(404, "图片序号越界")
    dl = _read_downloader(ad, source, comic_id)
    try:
        data, _cp = dl.get(imgs[idx], comic_id, chapter_id, idx)
    except Exception:
        # 回源失败但磁盘上已有可用文件（预热/上一次请求已落盘）→ 直出磁盘：
        # 文件明明在本地却报"图片失败"，对用户是说不通的
        _d3 = _manga_media_root(source, comic_id, chapter_id)
        if os.path.isdir(_d3):
            _hit3 = _serve_local_image(_d3, idx, ad)
            if _hit3 is not None:
                return _hit3
        raise
    if data is None:
        abort(500, "图片下载失败")
    if _cp and os.path.exists(_cp):
        from flask import send_from_directory
        try:
            _ext = os.path.splitext(_cp)[1].lower()
            resp = send_from_directory(
                os.path.dirname(_cp), os.path.basename(_cp),
                mimetype=("image/" + _ext.lstrip(".")) if _ext else "image/webp",
                conditional=True, max_age=7 * 86400)
            resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
            return resp
        except Exception:
            pass
    return send_file(_io.BytesIO(data), mimetype="image/webp",
                     max_age=3600 * 24)


@bp.route("/api/manga/<source>/<comic_id>/chapter/<chapter_id>/img/<int:idx>")
def api_manga_image(source, comic_id, chapter_id, idx):

    source = _safe_seg(source, "漫画源")
    comic_id = _safe_comic_id(source, comic_id)
    chapter_id = _safe_seg(chapter_id, "章节")
    """读取/懒下载章节图片（本地优先：已下载直接读磁盘，零源站请求）"""
    _d = _manga_media_root(source, comic_id, chapter_id)
    ad = _manga_read_adapter(source)  # R22: 阅读通道实例（绕过源令牌桶）
    if os.path.isdir(_d):
        # 源仍可用时，旧处理版本不能被当作合法本地页直接发给读者；下载器会
        # 在下一次取图前清整章并按当前算法重建。源已移除时仍保留本地阅读。
        _hit = _serve_local_image(_d, idx, ad)
        if _hit is not None:
            return _hit
    def _stale_local_reason():
        """本地目录是旧版处理缓存吗？（源已下线/回源失败时都要给出同一个可行动原因）"""
        try:
            from engine.manga.manager import adapter_meta as _am4
            from engine.manga.downloader import _processed_marker_version as _pmv4
            _w4 = int((_am4(source) or {}).get("process_version") or 0)
            if not _w4:
                _w4 = int(getattr(ad, "PROCESS_VERSION", 0) or 0)
            if _w4 > 0 and os.path.isdir(_d):
                _m4 = _pmv4(_d, ad)
                if _m4 is None or not str(_m4).isdigit() or int(_m4) != _w4:
                    return (f"本章图片是旧版处理缓存（当前算法版本 {_w4}），需联网重新获取")
        except Exception:
            pass
        return ""

    def _stale_response(underlying=""):
        """本地是旧版缓存 → 409"需要联网重新获取"。

        `underlying`：本次回源失败的真实原因。必须带上——否则"解码失败/网络失败"
        会被伪装成"旧缓存需要重取"，用户与排障都会被误导（0.63.0 实测踩到：
        设备上 Pillow 缺 WebP 导致每张图解码失败，却显示成"旧版缓存"）。
        """
        _why = _stale_local_reason()
        if not _why:
            return None
        if underlying:
            _why += f"；本次回源失败：{underlying}"
        return jsonify({"error": _why, "code": "STALE_IMAGE_CACHE"}), 409

    if not ad:
        # 0.61.0（指南 §7）：源已下线 + 本地是**旧版处理缓存**时，不能报成
        # "图片不存在/解码失败"——用户需要知道的是"这张图要联网重新获取"。
        _stale_only = False
        try:
            from engine.manga.manager import adapter_meta as _am2
            from engine.manga.downloader import _processed_marker_version as _pmv2
            _w2 = int((_am2(source) or {}).get("process_version") or 0)
            if _w2 > 0 and os.path.isdir(_d):
                _m2 = _pmv2(_d, None)
                _stale_only = (_m2 is None or not str(_m2).isdigit() or int(_m2) != _w2)
        except Exception:
            _stale_only = False
        _r409 = _stale_response()
        if _r409 is not None:
            return _r409
        abort(404, "漫画源不存在(本地无此章节图片)")
    # P1-4 + B05: in-flight 单飞去重——同图并发请求只回源一次；
    # 跟随者等待 leader 同轮结果：成功→重查磁盘缓存直出；失败/超时→
    # 明确"暂不可用"，绝不绕过 leader 自行回源（不做第二个同图生产者）；
    # 失败仅秒级冷却，用户刷新即可重试（不长期固化"失败=无图"）。
    _sf_key = (source, comic_id, chapter_id, idx)
    with _img_fetching_lock:
        _fl = _img_fetching.get(_sf_key)
        if _fl is None:
            if time.time() - _img_fail_ts.get(_sf_key, 0) < _IMG_FAIL_COOLDOWN:
                return _err_json("图片暂不可用（刚获取失败，请稍后刷新重试）", 503)
            # 有界：先清挂死条目，超限再淘汰最旧
            _now = time.time()
            for _k, _v in list(_img_fetching.items()):
                if _now - _v.ts > _IMG_FETCH_STALE:
                    _v.done.set()
                    _img_fetching.pop(_k, None)
            while len(_img_fetching) >= _IMG_FETCH_MAX:
                _k = next(iter(_img_fetching))
                _img_fetching[_k].done.set()
                _img_fetching.pop(_k, None)
            _fl = _ImgFlight()
            _img_fetching[_sf_key] = _fl
            _leader = True
        else:
            _leader = False
    if not _leader:
        if _fl.done.wait(timeout=_IMG_FETCH_WAIT_TIMEOUT) and _fl.ok:
            # 领导者已写完磁盘缓存 → 本地重查（命中则零回源直出）
            _d2 = _manga_media_root(source, comic_id, chapter_id)
            if os.path.isdir(_d2):
                _hit2 = _serve_local_image(_d2, idx, ad)
                if _hit2 is not None:
                    return _hit2
            # 领导者成功但本地未命中（极小文件等边缘情形）：走冷却重试语义
        # 同轮失败 / 本地未命中 / 等待超时 → 明确"暂不可用"，不自行回源
        return _err_json("图片暂不可用（同图获取未完成，请刷新重试）", 503)
    try:
        _resp = _serve_remote_image(ad, source, comic_id, chapter_id, idx)
        _fl.ok = True
        return _resp
    except Exception as e:
        with _img_fetching_lock:
            _img_fail_ts[_sf_key] = time.time()
            # 失败时间戳表有界
            if len(_img_fail_ts) > 500:
                for _k in sorted(_img_fail_ts, key=lambda k: _img_fail_ts[k]
                                 )[:len(_img_fail_ts) - 500]:
                    _img_fail_ts.pop(_k, None)
        # 0.61.0（升级版 Venera 指南 §4 P0）：本地是**旧版处理缓存**且源可用时，
        # 图片不会直出、而是回源重建；回源失败（典型：无网络）必须告诉用户
        # "需要联网重新获取"，而不是含糊的"图片读取失败"（那会被当成解码坏了）。
        _r409b = _stale_response(f"{type(e).__name__}: {str(e)[:120]}")
        if _r409b is not None:
            return _r409b
        return _err_response(e, 500, "图片读取失败")
    finally:
        with _img_fetching_lock:
            if _img_fetching.get(_sf_key) is _fl:
                _img_fetching.pop(_sf_key, None)
        _fl.done.set()


# A01: 章节修复并发协调——同一章节同时只允许一个修复流程，
# 避免两份"覆盖重下"互相踩踏暂存目录与版本切换
_repair_locks = {}
_repair_locks_guard = threading.Lock()

_REPAIR_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _repair_fetch_pages(imgs, target_dir, indexes):
    """并发下载指定页到 target_dir（tmp+文件头校验+os.replace 原子落盘）。
    返回 (fixed, errs)。P0-1: fetch_image_checked 逐跳做
    url_is_public_resolved 校验（DNS 解析后必须全公网），禁自动重定向。"""
    from engine.manga.downloader import fetch_image_checked, atomic_write_image
    from concurrent.futures import ThreadPoolExecutor as _TPE
    _fixed = []
    _errs = []

    def _dl(i):
        try:
            r = fetch_image_checked(imgs[i], timeout=30,
                                    headers={"User-Agent": _REPAIR_UA,
                                             "Referer": "https://www.mangacopy.com/"})
            if r.status_code == 200 and len(r.content) > 1000:
                _ext = ".webp"
                _m = re.search(r"\.(jpe?g|png|webp)", imgs[i], re.I)
                if _m:
                    _ext = "." + _m.group(1).lower().replace("jpeg", "jpg")
                p = os.path.join(target_dir, f"{i:04d}{_ext}")
                # P2-7: tmp + 文件头校验 + os.replace 原子落盘
                atomic_write_image(p, r.content)
                # A01: 写盘成功校验——原子写返回后再确认文件真实存在且非坏图
                if not (os.path.exists(p) and os.path.getsize(p) > 1000):
                    return i, "写盘校验失败"
                return i, None
            return i, f"HTTP {r.status_code}"
        except SSRFBlocked as e:
            # 校验失败：记日志 + 失败条目（脱敏文案），不抛 500
            print(f"[manga-repair] 补页 {i} 地址被 SSRF 策略拒绝: {e}",
                  flush=True)
            return i, "目标地址被拒绝"
        except Exception as e:
            # P2-8: 异常原文只进日志，响应里只留异常类型名
            print(f"[manga-repair] 补页 {i} 失败: {type(e).__name__}: {e}",
                  flush=True)
            return i, type(e).__name__
    with _TPE(max_workers=3) as ex:
        for i, e in ex.map(_dl, indexes):
            if e:
                _errs.append((i, e))
            else:
                _fixed.append(i)
    return _fixed, _errs


def _repair_restore_backup(staging, target):
    backup = staging + ".old"
    if os.path.isdir(backup) and not os.path.exists(target):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.rename(backup, target)


# ══════════════════════════════════════════════════════════════
# 覆盖修复事务：同盘暂存 + 持久事务记录 + 启动恢复（2026-09-10）
#
# 背景：覆盖重下的版本切换是"旧目录改名备份 → 暂存改名正式"两次 rename。
# 两次之间崩溃只剩下一次处理（"下次同章修复时先恢复备份"），且暂存目录
# 若与目标不同盘，os.rename 会 EXDEV 失败或退化为非原子拷贝。
# 本层补齐三件事：
#   1. 暂存根目录优先选在**与目标章节目录同一文件系统**处（同盘 rename
#      才原子）；状态目录跨盘时改用 <downloads>/.repair_staging（同盘，
#      且位于 downloads 根下、任何 source 目录之外 → 章节扫描看不见）
#   2. 每次切换写**持久事务记录**（staged/backed_up/switched + 路径 +
#      页数），崩溃后可由记录判断该回滚还是该完成
#   3. recover_repair_transactions() 扫描未完成事务并收敛（启动时调用，
#      修复入口也会先对本漫画调用一次）
# ══════════════════════════════════════════════════════════════
REPAIR_TXN_SUFFIX = ".txn.json"

# 事务恢复全局互斥（P2）：同进程内串行化所有恢复调用。持锁方向恒为
# "章节修复锁 → 恢复锁(_repair_recover_guard) → 事务锁(_repair_txn_lock)"，
# 不存在反向持锁。**严禁在持有事务锁时调用全局 recover_repair_transactions**
# （那是 恢复锁→事务锁 的方向，反向调用即死锁）——需要复核时只能在事务锁内
# 直接调用内部 _repair_apply_locked。
_repair_recover_guard = threading.Lock()

try:                                   # POSIX 专属；缺失时退化为仅本地条带锁
    import fcntl as _fcntl
except ImportError:                    # pragma: no cover - 非 POSIX 兜底
    _fcntl = None

# 进程内实际互斥：**固定条带**的 RLock（按 staging 真实路径稳定映射到固定
# 槽位）。即使无 fcntl（非 POSIX），本地也有真实互斥，不会退化为"无锁"。
_REPAIR_LOCK_STRIPES = 64
_repair_local_locks = [threading.RLock() for _ in range(_REPAIR_LOCK_STRIPES)]


def _repair_local_lock(staging):
    """按 staging 真实路径映射到固定条带槽位的 RLock（进程内互斥实际锁）。

    hash() 在同一进程生命周期内对同一字符串稳定，故同章必落同一槽位；
    条带数固定，避免"每章一个锁对象"随章节数无界增长。"""
    key = os.path.realpath(staging) if staging else ""
    return _repair_local_locks[hash(key) % _REPAIR_LOCK_STRIPES]


@contextlib.contextmanager
def _repair_txn_lock(staging):
    """单章事务互斥：固定条带 RLock（进程内/跨线程）+ <staging>.txn.lock 上
    的 flock(LOCK_EX)（有 fcntl 时提供跨进程互斥）。

    写入者（_repair_overwrite_staged 的**整个 准备+下载+切换 周期**）与恢复者
    （_repair_apply_txn）都经此加锁，故二者不会读到对方中间态、不会交叉改名/
    删目录。**不得嵌套调用**：flock 按 fd 计，同线程再开一个 fd 会自阻塞。"""
    # 锁文件**故意不删**（flock 只借用文件本身；删掉会让新来的锁者另建文件、
    # 互斥失效），所以它必须放在"会被清理的目录"之外：
    #   · 旧实现放在 `<staging>.txn.lock`，而 staging 就在 `_repair_staging/` 下，
    #     既让用户数据目录里越积越多（每个作品一个），又有被暂存清理/rmtree 连带
    #     删掉的风险（删了 = 互斥窗口）。
    # 现在统一放到 `MANGA_STATE_DIR/_repair_locks/<staging 的哈希>.lock`。
    import hashlib as _hl
    _lock_dir = os.path.join(MANGA_STATE_DIR, "_repair_locks")
    try:
        os.makedirs(_lock_dir, exist_ok=True)
    except OSError:
        _lock_dir = os.path.dirname(staging) or "."
    lock_p = os.path.join(
        _lock_dir, _hl.sha1(os.path.abspath(staging).encode("utf-8")).hexdigest()[:24] + ".lock")
    local = _repair_local_lock(staging)
    local.acquire()
    fd = None
    try:
        if _fcntl is not None:
            fd = os.open(lock_p, os.O_CREAT | os.O_RDWR, 0o644)
            _fcntl.flock(fd, _fcntl.LOCK_EX)
        yield
    finally:
        if fd is not None:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            finally:
                os.close(fd)
        local.release()


def _repair_within(path, *roots):
    """path 是否为任一 root 的**真子路径**（解析符号链接）。

    - 越界、软链（含多级软链）逃逸一律判否；
    - path == root 本身亦判否——防止把受控根目录整体当暂存/备份删除；
    事务记录来自磁盘、可被篡改/损坏，其路径字段不可信，恢复前必须校验。
    """
    if not path:
        return False
    try:
        rp = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    for root in roots:
        if not root:
            continue
        try:
            rr = os.path.realpath(root)
        except (OSError, ValueError):
            continue
        rr = rr.rstrip(os.sep)
        if rr and rr != os.sep and rp.startswith(rr + os.sep):
            return True
    return False


def _repair_target_safe(target):
    """目标必须是 downloads 根下 source/comic/chapter 三级的**真子路径**。

    拒绝：等于 downloads 根、等于某 source 根、层级不足、软链逃逸、含
    `..` 逃逸。防止损坏/被篡改记录把"目标"指向书库根或 source 根而整目录
    被 rmtree/改名。"""
    if not target or not isinstance(target, str):
        return False
    try:
        rp = os.path.realpath(target)
        rd = os.path.realpath(MANGA_DOWNLOADS_DIR)
    except (OSError, ValueError):
        return False
    rd = rd.rstrip(os.sep)
    if not rd or rd == os.sep or not rp.startswith(rd + os.sep):
        return False
    rel = rp[len(rd) + 1:]
    parts = [p for p in rel.split(os.sep) if p not in ("", ".", "..")]
    return len(parts) >= 3


def _same_device(path_a, path_b):
    """两个路径是否在同一文件系统（决定 rename 是否原子）"""
    try:
        return os.stat(path_a).st_dev == os.stat(path_b).st_dev
    except OSError:
        return False


def _repair_staging_base(target_dir):
    """选定暂存根目录（必须与 target_dir 尽量同盘）。

    1) MANGA_STATE_DIR/_repair_staging —— 历史路径，单盘部署即为同盘；
    2) 若与目标不同盘 → MANGA_DOWNLOADS_DIR/.repair_staging —— 与章节目录
       同在 downloads 盘；以点开头且不在任何 source 目录内，章节扫描
       （遍历 downloads/<source>/<comic_id>/）不可见，不会产生幽灵章节。
    返回绝对路径（不创建）。
    """
    state_base = os.path.join(MANGA_STATE_DIR, "_repair_staging")
    probe_state = state_base if os.path.isdir(state_base) else MANGA_STATE_DIR
    probe_target = target_dir if os.path.isdir(target_dir) \
        else os.path.dirname(target_dir.rstrip(os.sep)) or MANGA_DOWNLOADS_DIR
    if _same_device(probe_state, probe_target):
        return state_base
    return os.path.join(MANGA_DOWNLOADS_DIR, ".repair_staging")


def _repair_txn_path(staging):
    return staging + REPAIR_TXN_SUFFIX


def _repair_txn_write(staging, target, state, total=0, extra=None):
    """原子写事务记录（失败抛 OSError 由调用方处理：切换前必须能落记录）"""
    payload = {"state": state, "staging": staging, "backup": staging + ".old",
               "target": target, "total": int(total),
               "ts": time.time(), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if extra:
        payload.update(extra)
    os.makedirs(os.path.dirname(staging), exist_ok=True)
    atomic_write(_repair_txn_path(staging), payload)


def _repair_txn_clear(staging):
    try:
        os.remove(_repair_txn_path(staging))
    except OSError:
        pass


def _repair_dir_pages(d):
    """目录内图片页数（用于判断暂存是否完整）"""
    try:
        return sum(1 for f in os.listdir(d)
                   if f[:4].isdigit() and f.lower().endswith(
                       (".webp", ".jpg", ".jpeg", ".png")))
    except OSError:
        return 0


def _repair_txn_roots(base):
    """恢复扫描根：显式 base 优先；否则两个候选根——历史状态目录与同盘
    downloads 点目录，兼容"暂存根在两者间切换"遗留的历史事务。"""
    if base:
        return [base]
    return [os.path.join(MANGA_STATE_DIR, "_repair_staging"),
            os.path.join(MANGA_DOWNLOADS_DIR, ".repair_staging")]


def _repair_page_is_image(path):
    """单页文件是否为有效图片：复用下载器同一 `_valid_image_header`
    （JPEG/PNG/GIF/WEBP/AVIF 魔数）——此前仅判 RIFF 4 字节过弱。"""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return False
    except OSError:
        return False
    from engine.manga.downloader import _valid_image_header
    return _valid_image_header(path)


def _repair_valid_idxs(d):
    """目录内**文件头有效**的图片索引集合——按命名索引**去重**
    （0000.webp 与 0000.jpg 视为同一页），仅认纯数字命名。"""
    try:
        names = os.listdir(d)
    except OSError:
        return set()
    seen = set()
    for f in names:
        stem = f.rsplit(".", 1)[0] if "." in f else f
        if not stem.isdigit():
            continue
        if _repair_page_is_image(os.path.join(d, f)):
            seen.add(int(stem))
    return seen


def _repair_valid_pages(d, expected):
    """目录是否含**完整**的合法图页集合 {0,1,...,expected-1}。

    - 按命名索引去重：同一 idx 的多种扩展名只算一页，杜绝"重复格式冒充多页"；
    - 每个 idx 的文件须通过 downloader._valid_image_header；
    - 必须完整覆盖 0..expected-1，缺号判否。"""
    if isinstance(expected, bool) or not isinstance(expected, int) \
            or expected <= 0:
        return False
    return set(range(expected)) <= _repair_valid_idxs(d)


def _repair_dir_complete(d, expected):
    """目录是否够 expected 张**完整有效**图——绝不能以"任意/重复 1 页"冒充完整。"""
    return _repair_valid_pages(d, expected)


def _repair_txn_read(txn_p):
    """读取并校验事务记录 schema。返回 (txn, None) 或 (None, 非法原因)。

    schema（记录来自磁盘、字段不可信，必须严格校验）：
      staging  必须等于记录文件名去掉 .txn.json（防记录指针改写路径）
      state    ∈ {staged, backed_up, switched}
      total    正整数（expected 页数，判定"完整"的唯一依据）
      target   非空字符串
    非法 → 只丢弃记录、绝不触碰任何路径。
    """
    try:
        with open(txn_p, encoding="utf-8") as f:
            txn = json.load(f)
    except (OSError, ValueError):
        return None, "记录解析失败"
    if not isinstance(txn, dict):
        return None, "记录结构非法"
    staging = txn.get("staging")
    if not isinstance(staging, str) or os.path.realpath(staging) != \
            os.path.realpath(txn_p[:-len(REPAIR_TXN_SUFFIX)]):
        return None, "记录 staging 与文件名不匹配"
    if txn.get("state") not in ("staged", "backed_up", "switched"):
        return None, "记录 state 非法"
    expected = txn.get("total")
    if isinstance(expected, bool) or not isinstance(expected, int) \
            or expected <= 0:
        return None, "记录 expected(total) 非法"
    target = txn.get("target")
    if not isinstance(target, str) or not target:
        return None, "记录 target 缺失"
    return {"staging": staging, "target": target,
            "state": txn["state"], "expected": expected}, None


def _repair_corrupt_txn(txn_p, report, reason="记录损坏"):
    """非法/损坏记录收敛：只丢记录、不碰任何路径；但若同目录存在非空暂存
    或备份（可能是**唯一完本**），一律保留记录与数据，绝不删除。"""
    staging = txn_p[:-len(REPAIR_TXN_SUFFIX)]
    backup = staging + ".old"
    if _repair_valid_idxs(backup) or _repair_valid_idxs(staging):
        report.append({"txn": txn_p,
                       "action": f"{reason}；存在非空暂存/备份 → 保留待恢复"})
        return
    try:
        os.remove(txn_p)
    except OSError:
        pass
    report.append({"txn": txn_p, "action": f"{reason}；无有价值数据 → 丢弃记录"})


def _repair_converge(txn_p, staging, backup, target, state, expected, report):
    """在事务锁内按 state + expected 收敛（规则见 recover 文档）。不抛异常。"""
    import shutil as _sh
    target_complete = _repair_dir_complete(target, expected)
    backup_ok = bool(_repair_valid_idxs(backup))   # 备份有任意有效页即可回滚
    try:
        if target_complete:
            # 目标 ≥ expected 张有效图 → 完成态，方清备份与暂存
            if os.path.isdir(backup):
                _sh.rmtree(backup, ignore_errors=True)
            _sh.rmtree(staging, ignore_errors=True)
            action = "目标完整 → 清理备份/暂存"
        elif backup_ok:
            # 目标缺失/页数不足/坏图 → 无法证明完整；备份是唯一可信完本，
            # 保守回滚（绝不清掉唯一旧完本）
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.isdir(target):
                _sh.rmtree(target, ignore_errors=True)
            os.rename(backup, target)
            _sh.rmtree(staging, ignore_errors=True)
            action = "目标不完整且有备份 → 回滚"
        elif _repair_dir_complete(staging, expected):
            # 目标与备份都不在、暂存够 expected 张有效图 → 完成切换
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.isdir(target):
                _sh.rmtree(target, ignore_errors=True)
            os.rename(staging, target)
            action = "暂存完整 → 完成切换"
        else:
            _sh.rmtree(staging, ignore_errors=True)
            action = "无可恢复内容 → 清理暂存"
    except OSError as e:
        report.append({"txn": txn_p, "action": f"恢复失败: {type(e).__name__}"})
        return
    _repair_txn_clear(staging)
    report.append({"txn": txn_p, "action": action, "target": target,
                   "state": state})


def _repair_apply_txn(txn_p, root, report):
    """在事务锁内收敛单条事务记录（txn_p 位于扫描根 root 内）。不抛异常。

    先按记录文件名推导 staging 取锁，**锁内重新读取并校验**后才动作：等锁
    期间记录可能已被另一写入者清除（无事务 → 直接返回）、被替换为新事务
    （按新记录收敛）或已被篡改（重新走 schema/路径校验），绝不基于锁外读到
    的陈旧快照改名/删目录。"""
    staging0 = txn_p[:-len(REPAIR_TXN_SUFFIX)]
    with _repair_txn_lock(staging0):
        _repair_apply_locked(txn_p, root, report)


def _repair_apply_locked(txn_p, root, report):
    """**必须已持有 <staging>.txn.lock** 时调用：锁内重读 + 校验 + 收敛。

    绝不在本函数内调用全局 recover_repair_transactions（避免 恢复锁↔事务锁
    反向持锁死锁）。任何异常不抛出。"""
    txn, reason = _repair_txn_read(txn_p)
    if txn is None:
        if not os.path.exists(txn_p):
            return                    # 等锁期间记录已消失 → 无事务
        staging0 = txn_p[:-len(REPAIR_TXN_SUFFIX)]
        if not _repair_within(staging0, root):
            report.append({"txn": txn_p,
                           "action": "记录位置越界 → 拒绝并丢弃记录"})
            try:
                os.remove(txn_p)
            except OSError:
                pass
            return
        _repair_corrupt_txn(txn_p, report, reason)
        return
    staging, target = txn["staging"], txn["target"]
    backup = staging + ".old"        # 恒定推导，不信任记录内的 backup 字段
    # 路径安全：暂存/备份须为扫描根的真子路径；target 须为 downloads 根下
    # source/comic/chapter 三级的真子路径。记录路径不可信，越界只丢记录、
    # 绝不触碰文件系统。
    if not _repair_within(staging, root) or not _repair_within(backup, root):
        report.append({"txn": txn_p, "action": "暂存路径越界 → 拒绝并丢弃记录"})
        try:
            os.remove(txn_p)
        except OSError:
            pass
        return
    if not _repair_target_safe(target):
        report.append({"txn": txn_p, "action": "目标路径越界 → 拒绝并丢弃记录"})
        try:
            os.remove(txn_p)
        except OSError:
            pass
        return
    _repair_converge(txn_p, staging, backup, target,
                     txn["state"], txn["expected"], report)


def recover_repair_transactions(base=None, only=None):
    """收敛未完成的覆盖修复事务，返回处理明细列表。

    状态机（记录里的 state 表示"已经完成到哪一步"）：
      staged    新版本已在暂存目录校验通过，尚未改名切换
      backed_up 旧目录已改名为备份
      switched  暂存已改名为正式目录
     收敛规则（"完整"= 记录 expected 页数下，按**命名索引去重**且完整覆盖
     0..expected-1 的合法图片集合；杜绝"任意/重复格式 1 页冒充完整"）：
      目标 ≥ expected 张有效图           → 清备份/暂存（完成态）
      目标不完整(缺失/页数不足/坏图) + 备份有效 → 备份回滚（旧完本唯一可信）
      目标/备份都不在 + 暂存 ≥ expected 张 → 完成切换
      其余                              → 清理暂存
    安全边界：
      * 记录 schema 严格校验（staging 与文件名一致、state 合法、total 正整
        数、target 非空）；非法只丢记录、不碰任何路径；
      * 暂存/备份须为扫描根真子路径；target 须为 downloads 根下三级真子路径
        ——越界、等于根/"source 根"、多级软链逃逸一律拒绝，绝不删除；
      * 目标"完整"要求页数达标且每页文件头有效，避免空/半成品目标被误判为
        完成而删掉唯一备份；
      * 非法/损坏记录只要有非空暂存/备份即保留，绝不删除唯一完本；
       * 每章事务经 <staging>.txn.lock 的 fcntl.flock（有 fcntl 时）与写入者
         跨进程互斥；进程内/跨线程由**固定条带 RLock** 提供真实本地互斥
         （无 fcntl 平台亦不退化为无锁）；写入者的**整个 准备+下载+切换
         周期**都持此锁；
       * 取锁后**在锁内重新读取并校验**记录再动作（等锁期间记录可能已消失/
         被换成新事务/被篡改）；仅在锁外先做一次 recover，绝不在持事务锁时
         调用全局 recover（避免 恢复锁↔事务锁 反向持锁死锁）；
      * 任何异常都不抛出，逐条记录结果，启动路径尽力而为。

    only: 仅收敛该暂存目录（绝对路径）对应的事务——运行期按章恢复使用，
          调用方已持本章修复锁，不会触碰其他章节正在进行的切换。
    """
    report = []
    with _repair_recover_guard:
        if only:
            txn_p = _repair_txn_path(only)
            if os.path.isfile(txn_p):
                _repair_apply_txn(txn_p, base or os.path.dirname(only), report)
        else:
            for root in _repair_txn_roots(base):
                if not root or not os.path.isdir(root):
                    continue
                for dirpath, _dirs, files in os.walk(root):
                    for fn in files:
                        if not fn.endswith(REPAIR_TXN_SUFFIX):
                            continue
                        _repair_apply_txn(os.path.join(dirpath, fn), root,
                                          report)
    if report:
        print(f"[manga-repair] 启动恢复处理 {len(report)} 条未完成事务: "
              f"{[r['action'] for r in report]}", flush=True)
    return report


def _repair_overwrite_staged(source, comic_id, chapter_id, _dir, imgs):
    """A01 覆盖重下 + 事务化切换（2026-09-10）：
    新版本写入**与目标同盘**的暂存目录，全量校验（清单非空 + 全部页下载
    成功 + 写盘成功）后，按持久事务记录的三个状态点原子切换；任何失败
    保留原章可继续阅读，只有切换完成才报告"修复完成"。
    崩溃后由 recover_repair_transactions() 依据记录收敛（回滚或完成）。"""
    import shutil
    total = len(imgs)
    # 暂存目录：优先与目标章节目录同盘（rename 才原子）；跨盘时退到
    # downloads 根下的 .repair_staging（同盘且不在任何 source 目录内，
    # 章节扫描/读图逻辑完全看不见半成品）
    staging = os.path.join(_repair_staging_base(_dir),
                           source, comic_id, chapter_id)
    # 锁外先按事务记录收敛本章旧账（recover 内部走 恢复锁 + 事务锁；绝不能
    # 在持有事务锁时调用全局 recover，避免 恢复锁↔事务锁 反向持锁死锁）。
    # 只收敛本章节（only=staging）：调用方已持本章修复锁，绝不触碰其他章节。
    try:
        recover_repair_transactions(only=staging)
    except Exception as e:      # 恢复失败不阻断本次修复（原章仍在时无影响）
        print(f"[manga-repair] 事务恢复异常: {type(e).__name__}: {e}",
              flush=True)
    # ⚠ 整个 准备 + 下载 + 切换 周期都在本章事务锁内：与另一进程的写入者/
    # 恢复者（含启动恢复）严格互斥——杜绝"锁外下载期间被并发恢复读到半成品
    # 而误判/误删"与"两写入者交叉改名/删目录"。锁在进程崩溃时由内核释放。
    with _repair_txn_lock(staging):
        # 锁内复核：等锁期间可能已被其他进程写入/替换旧记录。此处**直接调用
        # 内部 _repair_apply_locked**（绝不调用全局 recover，避免死锁）。
        try:
            _repair_apply_locked(_repair_txn_path(staging),
                                 os.path.dirname(staging), [])
        except Exception as e:
            print(f"[manga-repair] 锁内事务复核异常: {type(e).__name__}: {e}",
                  flush=True)
        # 收敛后若仍留有备份且正式目录缺失：备份是唯一完本 → 先回滚保住它，
        # 再开始新一次覆盖，绝不直接删掉旧数据
        _orphan_bak = staging + ".old"
        if os.path.isdir(_orphan_bak) and not os.path.isdir(_dir):
            os.makedirs(os.path.dirname(_dir), exist_ok=True)
            os.rename(_orphan_bak, _dir)
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(staging + ".old", ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        _fixed, _errs = _repair_fetch_pages(imgs, staging, range(total))
        # 完整校验：全部页下载成功 + 全部页写盘成功（按 idx 去重）
        _written = {int(f[:4]) for f in os.listdir(staging)
                    if f[:4].isdigit() and f.lower().endswith(
                        (".webp", ".jpg", ".jpeg", ".png"))}
        if _errs or len(_written) != total:
            shutil.rmtree(staging, ignore_errors=True)
            return jsonify({"ok": False, "overwrite": True, "total": total,
                            "failed": total - len(_written),
                            "error": "覆盖重下未完成，已保留原章节",
                            "errors": [f"{i}:{e}" for i, e in _errs[:10]]}), 500
        # 事务点①：新版本已就绪（此后崩溃 → 目标完整则清暂存，目标缺失
        # 且备份在则回滚，目标缺失且暂存完整则完成切换）
        try:
            _repair_txn_write(staging, _dir, "staged", total)
        except OSError as e:
            shutil.rmtree(staging, ignore_errors=True)
            return _err_response(e, 500, "暂存事务记录写入失败，原章节已保留")
        # 同盘校验：不同盘时 rename 会 EXDEV（或退化为非原子拷贝）→ 显式
        # 拒绝，保留原章与暂存，交由运维处理（_repair_staging_base 已避免）
        if os.path.isdir(_dir) and not _same_device(staging, _dir):
            _repair_txn_clear(staging)
            return _err_json("暂存目录与章节目录不在同一磁盘，已中止切换"
                             "（原章节未受影响）", 500)
        # 原子切换：旧目录改名备份 → 暂存改名正式；改名失败回滚备份
        _parent = os.path.dirname(_dir.rstrip(os.sep))
        if _parent:
            os.makedirs(_parent, exist_ok=True)
        _bak = staging + ".old"
        try:
            if os.path.isdir(_bak):
                shutil.rmtree(_bak)
            if os.path.isdir(_dir):
                os.rename(_dir, _bak)
                # 事务点②：旧版本已备份（目标此刻缺失，备份是唯一可读版本）
                try:
                    _repair_txn_write(staging, _dir, "backed_up", total)
                except OSError:
                    # 记录失败时立即回滚到原版本，避免"无记录的悬空备份"
                    os.rename(_bak, _dir)
                    raise
            try:
                os.rename(staging, _dir)
            except OSError:
                if os.path.isdir(_bak) and not os.path.isdir(_dir):
                    os.rename(_bak, _dir)   # 回滚：恢复原版本
                raise
        except OSError as e:
            shutil.rmtree(staging, ignore_errors=True)
            _repair_txn_clear(staging)
            return _err_response(e, 500, "版本切换失败，原章节已保留")
        # 事务点③：新版本已在位（此后崩溃 → 恢复时目标完整才清备份）。
        # 记录写失败不改正确性（目标预期完整），但必须**保留** backed_up
        # 记录与备份目录，交由下次恢复按"目标完整"清理——保持可恢复状态，
        # 绝不在记录未落盘时删掉唯一备份。
        try:
            _repair_txn_write(staging, _dir, "switched", total)
        except OSError as e:
            print(f"[manga-repair] switched 记录写入失败，目标已就位，"
                  f"保留备份待恢复清理: {type(e).__name__}", flush=True)
        else:
            shutil.rmtree(_bak, ignore_errors=True)
            _repair_txn_clear(staging)
    # B03: 覆盖修复完成 → 书库统计快照增量更新（该部重扫）
    _manga_stats_note_change(source, comic_id)
    return jsonify({"ok": True, "overwrite": True, "total": total,
                    "fixed": len(_fixed), "msg": "修复完成"}), 200


@bp.route("/api/manga/<source>/<comic_id>/chapter/<chapter_id>/repair",
           methods=["POST"])
def api_manga_chapter_repair(source, comic_id, chapter_id):
    """章节补页：本地已下载章节缺页时，用网页版(copymanga_web)全量提取
    对比补下缺失页(保留已有图,不重下)。适用于 APP 时代下载不全的章。
    overwrite=覆盖重下（A01）：先验证清单 → 暂存目录全量下载+校验 →
    原子切换；任何失败保留原章可继续阅读。"""
    source = _safe_seg(source, "漫画源")
    comic_id = _safe_comic_id(source, comic_id)
    chapter_id = _safe_seg(chapter_id, "章节")
    if source not in ("copymanga", "copymanga_web"):
        return _err_json("仅支持拷贝漫画")
    data = request.get_json(silent=True) or {}
    overwrite = bool(data.get("overwrite"))
    _dir = _manga_media_root(source, comic_id, chapter_id)
    if not os.path.isdir(_dir) and not overwrite:
        return _err_json("本地无此章节，无法补页（请直接下载）", 404)
    if overwrite:
        # A01: 与下载单飞协调——同漫画下载/排队中拒绝覆盖，
        # 避免下载 worker 与版本切换同时写章节目录
        _dl_st = _manga_dl.status(_manga_dl_key(source, comic_id))
        if _dl_st.get("status") in ("running", "queued"):
            return _err_json("该漫画有下载任务进行中，请先暂停或等待完成后再覆盖重下",
                             409)
    # A01: 同章修复单飞（含补页），持锁期间独占暂存目录与版本切换
    # P1-1: 查表 + 非阻塞占用必须在 _repair_locks_guard 内一次完成。
    # 此前 lookup 与 acquire 分两步：线程 A 在 guard 内取到锁对象、尚未
    # acquire 时，线程 B 可释放并回收该对象，A 仍能对已脱管的旧锁
    # acquire 成功；与此同时线程 C 新建同一 _rk 的锁并成功占用 →
    # B/C 同时进入临界区（回收竞态双开，与"同章单飞"矛盾）。
    _rk = (source, comic_id, chapter_id)
    with _repair_locks_guard:
        _lock = _repair_locks.get(_rk)
        if _lock is None:
            _lock = threading.Lock()
            _repair_locks[_rk] = _lock
        if not _lock.acquire(blocking=False):
            # 已被其他线程持有（持有时该对象必在表中，不会被回收）
            return _err_json("该章节正在修复中，请稍后再试", 409)
    try:
        if overwrite:
            # 事务化暂存路径（与目标同盘优先）；历史崩溃残留先按记录收敛
            staging = os.path.join(_repair_staging_base(_dir),
                                   source, comic_id, chapter_id)
            try:
                recover_repair_transactions(only=staging)
            except Exception as e:
                print(f"[manga-repair] 事务恢复异常: {type(e).__name__}: {e}",
                      flush=True)
            try:
                _repair_restore_backup(staging, _dir)
            except OSError as e:
                return _err_response(e, 500, "旧章节恢复失败，备份已保留")
        # A01: 先取得并验证图片清单——清单失败/为空时原章分毫不动
        try:
            from engine.manga.copymanga_web import CopyMangaWeb
            from engine.manga.manager import register_adapter, get_adapter
            register_adapter(CopyMangaWeb)
            _wa = get_adapter("copymanga_web", state_dir=MANGA_STATE_DIR)
            imgs = _wa.images(comic_id, chapter_id)   # 网页全量(已滤广告)
        except Exception as e:
            return _err_response(e, 500, "网页取图失败")
        if not imgs:
            return _err_json("网页版未提取到图片", 500)
        if overwrite:
            return _repair_overwrite_staged(source, comic_id, chapter_id,
                                            _dir, imgs)
        # 补页模式：只下缺失页，已有图不动（失败不损失已有内容）
        if not os.path.isdir(_dir):
            os.makedirs(_dir, exist_ok=True)
        have = {int(f[:4]) for f in os.listdir(_dir)
                if f[:4].isdigit() and f.lower().endswith(
                    (".webp", ".jpg", ".jpeg", ".png"))}
        total = len(imgs)
        missing = [i for i in range(total) if i not in have]
        if not missing:
            return jsonify({"ok": True, "total": total,
                            "fixed": 0, "msg": "章节已完整"}), 200
        _fixed, _errs = _repair_fetch_pages(imgs, _dir, missing)
        # 同步已下载章节判定缓存(optional: 更新 _info.json downloaded 由扫描自动)
        # B03: 补页完成 → 书库统计快照增量更新（该部重扫）
        if _fixed:
            _manga_stats_note_change(source, comic_id)
        return jsonify({"ok": True, "total": total,
                        "had": len(have), "missing": len(missing),
                        "fixed": len(_fixed),
                        "errors": [f"{i}:{e}" for i, e in _errs[:10]]}), 200
    finally:
        # P1-1: 释放与回收在同一 guard 内原子完成——与占用端"lookup+acquire
        # 同 guard"配对后，不再存在"锁已释放但表项未清、他人误取旧对象"的窗口。
        # 回收未持有的锁对象，避免 _repair_locks 随修复过的章节数无界增长。
        with _repair_locks_guard:
            _lock.release()
            if _repair_locks.get(_rk) is _lock:
                _repair_locks.pop(_rk, None)


@bp.route("/api/manga/<source>/<comic_id>/chapter/<chapter_id>/proxy")
def api_manga_proxy(source, comic_id, chapter_id):

    source = _safe_seg(source, "漫画源")
    comic_id = _safe_comic_id(source, comic_id)
    chapter_id = _safe_seg(chapter_id, "章节")
    """CDN 图片代理：浏览器直连失败时的降级通道
    请求: ?u=<源站图片URL>（URL 编码）
    带源站 Referer 防盗链 + 服务器中转下载，缓存到本地
    """
    u = request.args.get("u", "")
    # R29(技术评审7.1): SSRF 防护——代理只允许公网 http/https 目标
    try:
        u = _safe_target_url(u, "图片URL")
    except ValueError:
        abort(400, "非法 URL")
    ad = _manga_adapter(source)
    if not ad:
        abort(404, "漫画源不存在")
    import hashlib
    import io as _io
    from flask import send_file as _send_file
    _tag = hashlib.md5(u.encode()).hexdigest()[:16]
    _dir = os.path.join(MANGA_CACHE_DIR, source, comic_id, chapter_id)
    os.makedirs(_dir, exist_ok=True)
    _cp = os.path.join(_dir, f"proxy_{_tag}.webp")
    if os.path.exists(_cp) and os.path.getsize(_cp) > 1000:
        return _send_file(_cp, mimetype="image/webp", max_age=7 * 86400)
    try:
        # P0-1: fetch_image_checked 内部完成 url_is_public_resolved
        # （DNS 解析后必须全公网）+ 禁自动重定向逐跳校验——封
        # "域名解析到内网"与"公网 URL 302 跳内网"两类 SSRF 绕过
        from engine.manga.downloader import fetch_image_checked
        _h = dict(ad.image_headers(u))
        _h.setdefault("User-Agent",
                      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36")
        _resp = fetch_image_checked(u, _h, timeout=20)
        if _resp.status_code != 200:
            abort(502, f"CDN {_resp.status_code}")
        _data = _resp.content
        if len(_data) < 500:
            abort(502, "图片过小")
        # 混淆源（jm）：代理通道同样还原块倒序（浏览器直连失败降级时图片不乱序）。
        # 还原失败**必须报错**，不能把乱序图当成功写进缓存（0.51.0 风险评估 P0-1）：
        # 读端一旦落盘，用户看到的就是花图，而且这份错误内容会一直被复用。
        _post = getattr(ad, "unscramble_image", None)
        if _post:
            try:
                _data = _post(_data, chapter_id, u)
            except Exception as e:
                abort(502, f"图片块还原失败：{e}")
            try:
                from engine.manga.downloader import mark_processed
                mark_processed(os.path.dirname(os.path.dirname(_cp)), ad,
                               comic_id, chapter_id)
            except Exception:
                pass
        # P2-7: tmp + os.replace 原子落盘——缓存命中只查文件存在性，
        # 直写 _cp 会让并发读者读到半图；失败清理 tmp
        _tmp = _cp + ".tmp"
        try:
            with open(_tmp, "wb") as _f:
                _f.write(_data)
            os.replace(_tmp, _cp)
        except BaseException:
            try:
                os.unlink(_tmp)
            except OSError:
                pass
            raise
        return _send_file(_io.BytesIO(_data), mimetype="image/webp",
                          max_age=7 * 86400)
    except SSRFBlocked as _e:
        # 半可信图片 URL 被安全策略拒绝：记日志 + 脱敏 502，不上抛 500
        print(f"[manga-proxy] SSRF 拦截: {_e}", flush=True)
        return _err_response(_e, 502, "图片代理失败")
    except Exception as _e:
        return _err_response(_e, 502, "图片代理失败")


