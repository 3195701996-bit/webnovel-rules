# -*- coding: utf-8 -*-
"""漫画源**功能级**验证（对照诊断 §6：导入依赖成功 / 依赖在，都不等于业务成功）。

对每个已注册的漫画适配器实际跑一遍：
    搜索 → 详情（含目录） → 章节图片 URL → **真实取一张图字节**
逐阶段记录耗时与证据。判定：

  verified     四步都拿到真实数据，且图片字节大于下限（真能看）
  partial      搜索/详情通过，但目录或取图失败（写明哪一阶段）
  failed       搜索阶段就失败
  unsupported  能力台账判定缺依赖（如 copymanga_web 需要 Playwright），或适配器未注册
  skipped      本轮按"近期已通过"跳过

能力台账（server/capabilities.py）只做依赖级判定且 verified 恒为 False；
本模块用**实测结果**回答"这个源到底能不能用"，两者互补、不互相冒充。
"""
import json
import os
import sys
import threading
import time
import traceback

from engine import source_verify as _sv
from engine.config import MANGA_DIR, MANGA_STATE_DIR

VERIFIED_FILE = os.path.join(MANGA_DIR, "_source_verified.json")

STAGES = ("search", "detail", "images")

DEFAULT_BUDGET = {"search": 20, "detail": 25, "images": 25, "image_bytes": 25}

# 图片字节下限：服务端本地直出用 1000 字节做判据，这里保持一致
MIN_IMAGE_BYTES = 1000

# 图片魔数：只凭"字节数够大"会把 HTML 报错页也当图片（实测踩到拿 Response 当 bytes，
# 以及 40 张图全部取图失败却差点被当成"有图"）。这里按真实文件头判定，
# 并把识别到的格式写进证据。
_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)


def sniff_image(data):
    """返回图片格式名，识别不出返回 ""（调用方据此判失败，不猜）。"""
    if not data:
        return ""
    for magic, name in _IMAGE_MAGIC:
        if data.startswith(magic):
            return name
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) > 12 and data[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    return ""
MAX_LIMIT = 12
DEFAULT_LIMIT = 4

_lock = threading.Lock()
_job = {"status": "idle", "done": 0, "total": 0, "current": "", "keyword": "",
        "started_at": "", "finished_at": "", "error": ""}


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _last_frame():
    """异常最后一帧（文件:行 函数），用于定位失败点——只给一句截断报错没法排查。"""
    try:
        tb = traceback.extract_tb(sys.exc_info()[2])
        if not tb:
            return ""
        f = tb[-1]
        return f"{os.path.basename(f.filename)}:{f.lineno} {f.name}"
    except Exception:
        return ""


def registered_keys():
    """当前实际注册的适配器 key（_load_manga_adapters 之后才准）。"""
    try:
        from server.state import _load_manga_adapters
        _load_manga_adapters()
    except Exception:
        pass
    try:
        from engine.manga.manager import list_adapters
        return [a["key"] for a in list_adapters()]
    except Exception:
        return []


def classify(stages, unsupported_reason=""):
    if unsupported_reason:
        return "unsupported", unsupported_reason
    s = stages.get("search") or {}
    if not s.get("ok"):
        return "failed", s.get("detail") or "搜索失败"
    d = stages.get("detail") or {}
    if not d.get("ok"):
        return "partial", "详情/目录阶段失败：" + (d.get("detail") or "无目录")
    i = stages.get("images") or {}
    if not i.get("ok"):
        return "partial", "取图阶段失败：" + (i.get("detail") or "无图片")
    return "verified", ""


# 硬性时限与"选书"口径统一由 engine/app_utils 提供（两个验证流程共用一份实现）。
from engine.app_utils import StageTimeout as _StageTimeout   # noqa: E402
from engine.app_utils import call_bounded as _shared_call_bounded


def call_bounded(fn, seconds):
    """薄封装：把共享实现包成 StageTimeout（漫画验证的历史异常名）。

    抽到 app_utils 的原因：小说侧 `engine.source_verify` 有**同一个**缺陷
    （适配器 timeout 被多地址乘出来 + 换关键词重试），各写一份必然漂移。
    """
    return _shared_call_bounded(fn, seconds)


def verify_one(key, keyword=None, budget=None, caps=None, now=_now):
    """验证单个漫画源。任何异常都转成阶段失败，不向外抛。"""
    b = dict(DEFAULT_BUDGET)
    b.update(budget or {})
    out = {"key": key, "name": key, "status": "failed", "reason": "",
           "tested_at": now(), "stages": {}, "keyword": keyword or ""}

    # 1) 能力台账：缺依赖的直接如实标 unsupported，不做"换个通道偷偷试"
    from server import capabilities as _caps
    st = _caps.source_status(key, caps=caps)
    out["ledger"] = {"status": st.get("status"), "reason": st.get("reason", ""),
                     "transport": st.get("transport", {})}
    if st.get("status") == "unsupported":
        out["status"] = "unsupported"
        out["reason"] = st.get("reason") or "能力台账判定不可用"
        return out

    from engine.manga.manager import get_adapter
    try:
        adapter = get_adapter(key, state_dir=MANGA_STATE_DIR)
    except Exception as e:
        adapter = None
        out["reason"] = f"适配器实例化失败：{type(e).__name__}"
    if adapter is None:
        out["status"] = "unsupported"
        out["reason"] = out["reason"] or "适配器未注册（能力台账标为待验证）"
        return out
    out["name"] = getattr(adapter, "name", "") or key

    kw = keyword or "巨人"
    kw_used = kw
    search_trace = ""

    # 2) 搜索
    #
    # **换关键词只在"没搜到"时才有意义**：源站连不上/超时的话，换 4 个词就是
    # 把同一段超时等 4 遍。2026-09-17 实测（nhentai 在本机不可达）：单源验证因此
    # 耗了整整 **120 秒** —— 手机上一按「逐源校验」就要干等两分钟。
    # 现在：连接类/超时类失败立即收尾并如实记录，不再换词硬试。
    t0 = time.monotonic()
    comics, err = [], ""
    _hard_fail = False
    for k in [kw] + [x for x in ("巨人", "海贼王", "火影忍者") if x != kw]:
        try:
            comics = call_bounded(lambda kk=k: adapter.search(kk, page=1),
                                  b.get("search", 20)) or []
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:100]}"
            comics = []
            from engine import neterr as _ne
            # 两种"换词也没用"的失败：连接类/超时类，以及我们自己的阶段超时
            if isinstance(e, _StageTimeout) or _ne.classify(e):
                _hard_fail = True
        if comics:
            kw_used = k
            break
        if _hard_fail:
            break
        err = err or "搜索无结果"
    out["keyword"] = kw_used
    ms = int((time.monotonic() - t0) * 1000)
    if not comics:
        out["stages"]["search"] = {"ok": False, "ms": ms, "detail": err or "搜索无结果",
                                   "trace": search_trace}
        out["status"], out["reason"] = "failed", out["stages"]["search"]["detail"]
        return out
    c0 = comics[0]
    out["stages"]["search"] = {
        "ok": True, "ms": ms, "count": len(comics),
        "detail": f"搜索到 {len(comics)} 部",
        "sample": {"id": getattr(c0, "id", ""), "title": getattr(c0, "title", "")},
    }
    comic_id = getattr(c0, "id", "")

    # 3) 详情 + 目录
    t1 = time.monotonic()
    detail, chapters, derr = None, [], ""
    detail_trace = ""
    js_required = False
    try:
        detail = call_bounded(lambda: adapter.comic_info(comic_id),
                              b.get("detail", 25))
        chapters = list(getattr(detail, "chapters", None) or [])
        if not chapters:
            chapters = call_bounded(lambda: adapter.chapters(comic_id),
                                    b.get("detail", 25)) or []
        if not chapters:
            derr = "目录为空"
    except Exception as e:
        from engine.manga.base import JsRequiredError
        if isinstance(e, JsRequiredError):
            js_required = True
        derr = f"{type(e).__name__}: {str(e)[:160]}"
        detail_trace = _last_frame()
    ms = int((time.monotonic() - t1) * 1000)
    if not chapters:
        out["stages"]["detail"] = {"ok": False, "ms": ms, "detail": derr or "无目录",
                                   "trace": detail_trace}
        if js_required:
            # 需要执行 JS 的源：按"未支持"如实归类，并保留搜索阶段证据
            out["status"] = "unsupported"
            out["reason"] = derr.replace("JsRequiredError: ", "")
            out["search_ok"] = True
            return out
        out["status"], out["reason"] = classify(out["stages"])
        return out
    out["stages"]["detail"] = {
        "ok": True, "ms": ms, "count": len(chapters),
        "detail": f"目录 {len(chapters)} 话",
        "has_info": bool(detail is not None),
    }
    ch0 = chapters[0]
    chapter_id = getattr(ch0, "id", "")

    # 4) 章节图片 URL + 真实取一张图（在线阅读能力的硬证据）
    t2 = time.monotonic()
    urls, ierr = [], ""
    images_trace = ""
    try:
        urls = list(call_bounded(lambda: adapter.images(comic_id, chapter_id),
                                 b.get("images", 25)) or [])
        if not urls:
            ierr = "图片列表为空"
    except Exception as e:
        ierr = f"{type(e).__name__}: {str(e)[:160]}"
        images_trace = _last_frame()
    ms = int((time.monotonic() - t2) * 1000)
    if not urls:
        out["stages"]["images"] = {"ok": False, "ms": ms, "detail": ierr or "图片列表为空",
                                   "trace": images_trace}
        out["status"], out["reason"] = classify(out["stages"])
        return out

    t3 = time.monotonic()
    nbytes, berr, fmt, code = 0, "", "", 0
    bytes_trace = ""
    try:
        from engine.manga.downloader import fetch_image_checked
        u = adapter.image_url(urls[0])
        headers = adapter.image_headers(urls[0]) or {}
        # 注意：fetch_image_checked 返回的是**响应对象**（.status_code/.content），
        # 不是 bytes——早先按 bytes 处理导致 TypeError（实测踩到，MangaDex 因此被误判失败）。
        resp = fetch_image_checked(u, headers, timeout=b["image_bytes"])
        code = int(getattr(resp, "status_code", 200) or 0)
        data = getattr(resp, "content", None)
        if data is None:
            data = resp if isinstance(resp, (bytes, bytearray)) else b""
        data = bytes(data)
        if code and code != 200:
            berr = f"HTTP {code}"
        nbytes = len(data)
        fmt = sniff_image(data)
        if not berr and nbytes < MIN_IMAGE_BYTES:
            berr = f"图片过小（{nbytes} 字节）"
        if not berr and not fmt:
            # 拿到的不是图片（例如风控 HTML 页）：字节数再大也不算通过
            berr = f"响应不是图片（{nbytes} 字节，文件头 {data[:8].hex()}）"
    except Exception as e:
        berr = f"{type(e).__name__}: {str(e)[:160]}"
        bytes_trace = _last_frame()
    ms = int((time.monotonic() - t3) * 1000)
    if berr:
        out["stages"]["images"] = {"ok": False, "ms": ms, "count": len(urls),
                                   "bytes": nbytes, "http": code,
                                   "detail": f"{len(urls)} 张，但取图失败：{berr}",
                                   "trace": bytes_trace}
    else:
        out["stages"]["images"] = {"ok": True, "ms": ms, "count": len(urls),
                                   "bytes": nbytes, "format": fmt, "http": code,
                                   "detail": f"{len(urls)} 张，首图 {nbytes} 字节（{fmt}）"}
    out["status"], out["reason"] = classify(out["stages"])
    return out


def save_merged(data):
    """按 key 合并写入（分批验证不冲掉上一批结论）。"""
    old = load_results() or {}
    merged = {}
    for it in old.get("items") or []:
        k = it.get("key") or ""
        if k:
            merged[k] = it
    for it in data.get("items") or []:
        k = it.get("key") or ""
        if k:
            merged[k] = it
    out = dict(data)
    out["items"] = list(merged.values())
    _sv._atomic_write(VERIFIED_FILE, out)
    return out


def load_results():
    try:
        with open(VERIFIED_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def results_payload():
    data = load_results()
    items = (data or {}).get("items") or []
    registered = registered_keys()
    known = {it.get("key") or "" for it in items}
    summary = _sv.summary_of(items, enabled_uids=registered, all_uids=registered)
    summary["registered_total"] = len(registered)
    summary["never_attempted"] = len([k for k in registered if k not in known])
    summary["pass_rate"] = (round(summary["verified"] * 100 / len(registered))
                            if registered else 0)
    return {"tested_at": (data or {}).get("tested_at", ""),
            "keyword": (data or {}).get("keyword", ""),
            "items": items, "total": len(items), "summary": summary}


def status():
    with _lock:
        return dict(_job)


def start(keyword=None, keys=None, limit=None, offset=0, skip_verified_days=7,
          skip_tested_days=0, budget=None, now=_now):
    """启动一次漫画源验证（后台线程）。并发调用返回 already_running。"""
    global _job
    keys_all = registered_keys()
    if keys:
        want = set(keys)
        keys_all = [k for k in keys_all if k in want]
    lim = limit if isinstance(limit, int) and limit > 0 else DEFAULT_LIMIT
    lim = min(lim, MAX_LIMIT)

    known = {it.get("key") or "": it for it in (load_results() or {}).get("items", [])}
    recent = [k for k in keys_all
              if _sv._verified_recently(k, skip_verified_days, known)
              or (skip_tested_days > 0 and _sv._tested_recently(k, skip_tested_days, known))]
    pool = [k for k in keys_all if k not in set(recent)]
    never = [k for k in pool if k not in known]
    retry = [k for k in pool if k in known]
    ordered = never + retry
    off = offset if isinstance(offset, int) and offset > 0 else 0
    targets = ordered[off:off + lim]

    with _lock:
        if _job.get("status") == "running":
            return {"started": False, "already_running": True, **_job}
        _job = {"status": "running", "done": 0, "total": len(targets) + len(recent),
                "current": "", "keyword": keyword or "", "started_at": now(),
                "finished_at": "", "error": "", "skipped_verified": len(recent),
                "offset": off}

    def _run():
        global _job
        items = [{"key": k, "name": known.get(k, {}).get("name") or k, "status": "skipped",
                  "reason": f"最近 {skip_verified_days} 天内已验证通过，本轮不重复验证",
                  "tested_at": known.get(k, {}).get("tested_at") or now(),
                  "stages": known.get(k, {}).get("stages") or {},
                  "keyword": known.get(k, {}).get("keyword") or ""}
                 for k in recent]
        try:
            for i, k in enumerate(targets):
                with _lock:
                    _job["current"] = k
                try:
                    items.append(verify_one(k, keyword=keyword, budget=budget))
                except BaseException as e:
                    items.append({"key": k, "name": k, "status": "failed",
                                  "reason": f"{type(e).__name__}: {str(e)[:100]}",
                                  "tested_at": now(), "stages": {}})
                with _lock:
                    _job["done"] = len(recent) + i + 1
                # 增量落盘：批次被系统杀掉也要留下已验结论（同小说验证）
                try:
                    save_merged({"tested_at": now(), "keyword": keyword or "",
                                 "profile": os.environ.get("WR_PROFILE", "desktop"),
                                 "items": items})
                except Exception as _e:
                    print(f"[manga-verify] 增量落盘失败: {type(_e).__name__}: {_e}", flush=True)
            save_merged({"tested_at": now(), "keyword": keyword or "",
                         "profile": os.environ.get("WR_PROFILE", "desktop"),
                         "items": items})
            with _lock:
                _job["status"] = "done"
                _job["finished_at"] = now()
                _job["current"] = ""
        except BaseException as e:
            with _lock:
                _job["status"] = "error"
                _job["error"] = f"{type(e).__name__}: {str(e)[:120]}"
                _job["finished_at"] = now()

    threading.Thread(target=_run, daemon=True, name="manga-source-verify").start()
    return {"started": True, **status()}


def reset_for_tests():
    """仅供测试：清内存态。"""
    global _job
    with _lock:
        _job = {"status": "idle", "done": 0, "total": 0, "current": "", "keyword": "",
                "started_at": "", "finished_at": "", "error": ""}
