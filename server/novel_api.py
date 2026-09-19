# -*- coding: utf-8 -*-
"""server.novel_api —— 小说域 API（书源/搜索/任务/书籍）。Blueprint: novel"""
import os
import re
import json
import time
import shutil
import hashlib
import threading

from flask import Blueprint, jsonify, request, Response, abort, send_file

from engine import source_verify
from engine.source_mgr import (load_all, load_enabled, get_by_uid, find_source,
                               import_sources, delete_source, set_enabled,
                               validate_source)
from engine.crawler import SourceCrawler
from engine.crawler import (export_is_fresh as _export_is_fresh,
                            merge_book_txt as _merge_book_txt)
from engine.app_utils import (now_iso, _norm, atomic_write, atomic_write_text,
                              load_json, _read_json, update_json,
                              cache_key_of as _cache_key)
from engine.config import (TOC_CACHE_TTL, SLOW_COOLDOWN,
                           SEARCH_MAX_WORKERS, SEARCH_SOURCE_TIMEOUT,
                           STREAM_STOP_EXACT, BOOK_PROGRESS_FILE,
                           ST_RUNNING)
from engine.urlsec import safe_target_url as _safe_target_url

# 2026-09-15（真断网）：小说端与漫画端同一口径——多个源报"本机网络"类失败且
# 实测没有路由时提前结束搜索，不让用户对着"搜索中"干等源站期限（见 engine/neterr.py）
NOVEL_OFFLINE_GRACE = 3.0
NOVEL_OFFLINE_MIN_FAILS = 2

# 2026-09-15（耐心上限，与漫画端 _MANGA_PATIENCE 同一口径）：
# 小说搜索此前只在"精确命中够多"或"总时限到"时收尾——一个卡死的源会让流一直挂到
# 它的单源时限（手机 9s、桌面 6s，卡在 deadline 覆盖不到的路径还要等 +2s），
# 用户明明已经看到结果，汇总行却迟迟不出现。现在：**已有结果**时最多再等这么久就收尾，
# 未返回的源照实写进 errors（区别于"真没结果"），并且这一轮的结果只**短暂**缓存
# （见 state._cache_ok 的 partial 处理），避免把"部分结果"当成一小时的完整结果。
NOVEL_PATIENCE = 6.0

from server.state import (
    STOP_KIND_USER_PAUSE, STOP_KIND_USER_STOP, _set_stop,
    DATA_DIR, BOOKS_DIR, TASKS_DIR,
    _tasks, _crawlers, _check_jobs, _check_missing,
    _toc_cache, _search_cache, _toc_lock, _toc_fetching, _lock,
    _slow_tracker, _slow_sources, _search_worker,
    SEARCH_CACHE_VERSION, _cache_ok, _err_response, _err_json,
    _search_cache_save, _toc_cache_save,
    _save_task, _log, _create_task, _launch_task, _task_snapshot,
    _run_check_update, _clean_book, _build_groups, _scan_books,
    _clean_caches, _toc_fetch_bounded, _TOC_FETCH_DEADLINE,
    _safe_str_arg, _safe_seg,
    load_book_state, invalidate_book_state,
    _manga_dl,
)

bp = Blueprint("novel", __name__)


# ══════════════════════════════════════════════
#  书源 API
# ══════════════════════════════════════════════
@bp.route("/api/sources")
def api_sources():
    """书源列表 + **移动可用性台账**字段（与 /api/manga/sources 同口径）。

    合并进来而不是让 App 再请求一次：书源页要显示"这个源到底能不能用、卡在哪一步"，
    分两个接口拿数据容易出现两处不一致（漫画侧就是合并的）。
    """
    from server import novel_catalog
    sources = load_all()
    try:
        cat = {r["uid"]: r for r in novel_catalog.build()["sources"]}
    except Exception:
        cat = {}
    for s in sources:
        c = cat.get(s.get("uid") or "")
        if not c:
            continue
        s["category"] = c["category"]
        s["category_label"] = c["category_label"]
        s["category_reason"] = c["reason"]
        s["failed_stage"] = c["failed_stage"]
        s["failed_stage_label"] = c["failed_stage_label"]
        s["verify_tested_at"] = c["tested_at"]
        s["verify_stale"] = c["stale"]
        s["verify_counts"] = c["counts"]
    return jsonify({"sources": sources})


@bp.route("/api/novel/catalog")
def api_novel_catalog():
    """小说源可用性台账（路线 P0-3 的纪律应用到小说侧）；?format=md 导出可归档表格。"""
    from server import novel_catalog
    payload = novel_catalog.build()
    if (request.args.get("format") or "").lower() == "md":
        return Response(novel_catalog.render_markdown(payload), mimetype="text/markdown")
    return jsonify(payload)


@bp.route("/api/sources/import", methods=["POST"])
def api_sources_import():
    """导入书源（JSON 文本 / 数组 / 包装对象），逐源校验"""
    data = request.get_json(silent=True) or {}
    if isinstance(data, list):
        payload = data
    elif isinstance(data, dict):
        payload = data.get("content") or data
    else:
        payload = data
    try:
        if isinstance(payload, str):
            payload = json.loads(payload)
    except Exception as e:
        # 脱敏：json 异常文本会回显 payload 片段/解析位置（P2-8 约定）
        return _err_response(e, 400, "JSON 解析失败")
    imported, results = import_sources(payload, validate=True)
    return jsonify({"imported": imported, "results": results})


@bp.route("/api/sources/export")
def api_sources_export():
    """导出书源为文件（路线 P1-2："SAF 导入导出"的导出侧）。

    形态（都可被 /api/sources/import 原样导回）：
      · ?uid=<uid>   → 单个源对象（Legado 格式）
      · 不带参数      → {"version":1,"exported_at":...,"sources":[...]}（全部源）

    为什么导 JSON 而不是 zip：书源本来就是 JSON，用户可以直接看、直接编辑，
    也能发给别人；zip 只在"含多个文件"时才有优势，而那正是数组能表达的。

    注意：只导出**书源配置**，不含阅读进度/正文/图片（那些走"备份与恢复"）。
    """
    from engine.source_mgr import load_all_with_files
    uid = _safe_str_arg("uid", maxlen=100)
    # load_all_with_files 返回的是**文件原始内容**（不含 uid：uid 在 load_all 里
    # 才按"文件名即 uid"补齐）。导出必须补上，否则单源导出永远 404、
    # 导出的文件也没有 uid 可依（实测踩到）。
    pairs = []
    for fn, data in load_all_with_files():
        if not isinstance(data, dict):
            continue
        data.setdefault("uid", fn[:-5])
        pairs.append(data)
    if uid:
        for data in pairs:
            if data.get("uid") == uid:
                return jsonify({"version": 1, "sources": [data]})
        return _err_json(f"书源不存在: {uid}", 404)
    items = pairs
    return jsonify({"version": 1,
                    "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "count": len(items),
                    "sources": items})


@bp.route("/api/sources/duplicates")
def api_sources_duplicates():
    """重复源文件（同一 uid 且同一站点地址的两份文件）与处置建议。

    说明：同一 uid 的两个文件会让"启用/删除"只作用于其中一个，用户看到的行为自相矛盾。
    这里给出**可执行**的结论（此前后端只说"建议去网页端清理"，等于让用户去用电脑）。
    安全口径见 engine.source_mgr.find_duplicate_sources：内容不一致的不提议、不自动动。
    """
    from engine.source_mgr import find_duplicate_sources
    return jsonify(find_duplicate_sources())


@bp.route("/api/sources/duplicates/cleanup", methods=["POST"])
def api_sources_duplicates_cleanup():
    """清理重复源文件：**只移动不删除**（移到数据目录下的 sources_removed/<时间戳>/）。

    请求体：{"files": ["a.json", "b.json"]}
    拒绝规则（不允许删任意文件）：
      · 文件名必须出现在**当前检测到的安全组**的 remove 列表里；
      · 路径必须落在书源目录内（复用 _source_path 的净化与边界校验）。
    """
    from engine.source_mgr import find_duplicate_sources, _source_path
    data = request.get_json(silent=True) or {}
    want = data.get("files") or []
    if not isinstance(want, list) or not want:
        return _err_json("缺少要清理的文件名列表")
    dup = find_duplicate_sources()
    allowed = {f for g in dup["groups"] if g.get("safe") for f in g["remove"]}
    picked = [str(x) for x in want if str(x) in allowed]
    refused = [str(x) for x in want if str(x) not in allowed]
    if not picked:
        return _err_json("这些文件不在可安全清理的重复列表里（内容可能不一致），已拒绝；"
                         "需要人工确认", 409)
    import shutil
    import time as _t
    from engine.config import DATA_DIR
    backup = os.path.join(DATA_DIR, "sources_removed",
                          _t.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(backup, exist_ok=True)
    moved = []
    for name in picked:
        try:
            src_path = _source_path(name[:-5] if name.endswith(".json") else name)
        except ValueError:
            refused.append(name)
            continue
        if not os.path.isfile(src_path):
            refused.append(name)
            continue
        try:
            shutil.move(src_path, os.path.join(backup, os.path.basename(src_path)))
            moved.append(os.path.basename(src_path))
        except Exception as e:
            print(f"[sources] 清理重复源失败 {name}: {e}", flush=True)
            refused.append(name)
    # 墓碑：这些文件是用户主动清掉的，**不许**被随包内置源在下次启动时又加回来
    # （实测：清掉 4 个重复文件后，下次启动又被解出 4 个——"删了又回来"）
    try:
        from engine.source_mgr import note_removed
        note_removed(moved, why="dup-cleanup", backup=backup)
    except Exception as _e:
        print(f"[sources] 记录清理墓碑失败: {_e}", flush=True)
    # 清理后使书源缓存失效，界面刷新立刻看到真实结果
    try:
        from engine.source_mgr import invalidate_sources_cache
        invalidate_sources_cache()
    except Exception:
        pass
    return jsonify({"ok": True, "moved": moved, "refused": refused,
                    "backup_dir": backup,
                    "remaining_groups": find_duplicate_sources()["safe_groups"]})


@bp.route("/api/sources/removed")
def api_sources_removed():
    """被清理/删除的源文件（备份批次）——给 App 提供"恢复"入口。

    清理只移动不删除是对的，但备份在应用私有目录里用户点不到，
    等于"清错了找不回来"；这里把可恢复的东西如实列出来。
    """
    from engine.source_mgr import list_removed_batches
    return jsonify(list_removed_batches())


@bp.route("/api/sources/removed/restore", methods=["POST"])
def api_sources_removed_restore():
    """恢复被清理/删除的源文件（可指定批次/文件名，或只恢复最近一批）。

    `latest=true`：只恢复最近一批 —— 客户端确认弹窗写的就是"最近一次清理/删除"；
    从前服务端**不读这个字段**，于是"恢复最近一次"实际把所有历史批次都恢复回来
    （连用户故意删掉的源也带回来并解除墓碑），与弹窗承诺相反。
    """
    from engine.source_mgr import restore_removed, list_removed_batches
    data = request.get_json(silent=True) or {}
    batch = (data.get("dir") or "").strip() or None
    names = data.get("files") or None
    if names is not None and not isinstance(names, list):
        return _err_json("files 必须是文件名数组")
    r = restore_removed(batch=batch, names=names,
                        latest=bool(data.get("latest")))
    left = list_removed_batches()
    return jsonify({"ok": True, **r,
                    "remaining": left["total"],
                    # 恢复后"最近一批"会变（空批次目录已被删掉），必须重取，
                    # 不能把恢复前的数字发回去
                    "latest_count": left["latest_count"]})


@bp.route("/api/sources/<uid>/validate", methods=["POST"])
def api_source_validate(uid):
    src = get_by_uid(uid)
    if not src:
        abort(404, "书源不存在")
    res = validate_source(src)
    # 更新保存：走 _source_path 统一净化 + realpath 边界校验，
    # 不再自行拼路径（避免绕过 _safe_uid 形成任意 .json 写入）
    from engine.source_mgr import _atomic_write, _source_path
    src["valid"] = res["ok"]
    src["valid_error"] = res.get("error", "")
    src["valid_tested_at"] = res.get("tested_at", "")
    try:
        _atomic_write(_source_path(uid), src)
    except ValueError:
        abort(400, "书源 UID 非法")
    # P2-2: 直写 sources/*.json 后主动失效书源缓存（mtime 指纹亦兜底）
    from engine.source_mgr import invalidate_sources_cache
    invalidate_sources_cache()
    return jsonify(res)


@bp.route("/api/sources/check-all", methods=["POST"])
def api_sources_check_all():
    """一键检验全部启用书源(异步): 立即返回, 逐源结果可经 /status 轮询"""
    # 判定 running 与置位必须在同一临界区：拆成两段时两个并发请求都能
    # 通过 running==False 判定，各起一个 worker 双写 results
    with _src_check_lock:
        if _src_check_state["running"]:
            return jsonify({"ok": False, "error": "检验已在进行中"}), 409
        _src_check_state.update(running=True, total=0, results=[],
                                started_at=time.time(), finished_at=0.0,
                                error="")
    try:
        srcs = load_enabled()
    except Exception:
        with _src_check_lock:
            _src_check_state["running"] = False
        raise
    if not srcs:
        with _src_check_lock:
            _src_check_state.update(running=False, finished_at=time.time())
        return jsonify({"ok": True, "total": 0, "results": []})
    with _src_check_lock:
        _src_check_state["total"] = len(srcs)
    try:
        threading.Thread(target=_src_check_worker, args=(srcs,),
                         daemon=True).start()
    except Exception as e:
        # 线程启动失败必须复位 running，否则一键检验从此恒定 409
        with _src_check_lock:
            _src_check_state["running"] = False
        return _err_response(e, 500, "检验任务启动失败")
    return jsonify({"started": True, "total": len(srcs)})


@bp.route("/api/sources/check-all/status")
def api_sources_check_all_status():
    """一键检验进度/结果(轮询)"""
    with _src_check_lock:
        st = dict(_src_check_state)
    results = st.pop("results", [])
    st["done"] = len(results)
    st["results"] = results
    return jsonify(st)


_src_check_state = {"running": False, "total": 0, "results": [],
                    "started_at": 0.0, "finished_at": 0.0, "error": ""}


_src_check_lock = threading.Lock()


def _src_check_worker(srcs):
    import concurrent.futures as _cf

    def _one(s):
        uid = s.get("uid", "")
        name = s.get("bookSourceName", "") or uid
        try:
            r = validate_source(s)
            return {"uid": uid, "name": name,
                    "ok": bool(r.get("ok")),
                    "error": r.get("error", ""),
                    "note": r.get("note", ""),
                    "sample": ((r.get("sample") or {}).get("name") or "")
                              + (" · " + ((r.get("sample") or {}).get("author") or "") if (r.get("sample") or {}).get("author") else ""),
                    "tested_at": r.get("tested_at", "")}
        except Exception as e:
            # 异常类型/文本只进日志：validate_source 的异常可能含源站 URL、
            # 本地路径，经 /status 原样吐出违背脱敏约定
            print(f"[src-check] {uid} 校验异常: {type(e).__name__}: {e}",
                  flush=True)
            return {"uid": uid, "name": name, "ok": False,
                    "error": "校验异常（详见服务端日志）",
                    "note": "", "sample": "", "tested_at": ""}

    # P3-3: executor 构建 / submit 也纳入最外层 try——此前在 try 之外，
    # 构建失败或 submit 抛错会直接跳过 finally，running 永久 True。
    ex = None
    _futs = []
    try:
        _dl = time.time() + 200   # R56: 整体 deadline 用 wait 周期检查——
        # as_completed 会在最后一个 future 卡住时永远等不到 deadline 判定
        try:
            ex = _cf.ThreadPoolExecutor(max_workers=8)
            _futs = [ex.submit(_one, s) for s in srcs]
            while _futs and time.time() < _dl:
                _done, _futs = _cf.wait(_futs, timeout=5,
                                        return_when=_cf.FIRST_COMPLETED)
                for f in _done:
                    try:
                        r = f.result()
                    except Exception:
                        continue
                    with _src_check_lock:
                        _src_check_state["results"].append(r)
        finally:
            for f in _futs:
                f.cancel()
            if ex is not None:
                ex.shutdown(wait=False)
    finally:
        # running 复位必须在最外层 finally（同 state.py 漫画侧修法）：
        # 构建/提交/wait/加锁/append 任一处非预期异常泄出时，否则 running
        # 永久为 True，check-all 从此恒定 409
        with _src_check_lock:
            _src_check_state["running"] = False
            _src_check_state["finished_at"] = time.time()


@bp.route("/api/sources/verify", methods=["POST"])
def api_sources_verify():
    """启动逐源功能级验证（搜索 → 详情 → 目录 → 正文）。

    异步：立即返回 202（或已有运行时返回 200 + already_running），
    结果经 /api/sources/verify/status 轮询，最后一次结果留在
    /api/sources/verify/results。未验证的源不会被标成通过（诊断 §6）。
    """
    data = request.get_json(silent=True) or {}
    limit = data.get("limit")
    try:
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None
    try:
        offset = int(data.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    # 注意：这里不能用 `or 7`——显式传 0（本轮忽略"近期已通过"）会被当成缺省值，
    # 实测踩到：批次想全量复核时仍按 7 天跳过，覆盖不足却看不出来。
    raw_days = data.get("skip_verified_days", 7)
    try:
        skip_days = int(7 if raw_days is None else raw_days)
    except (TypeError, ValueError):
        skip_days = 7
    # skip_tested_days：不管结果如何，最近 N 天内验过的都跳过——
    # 批次被系统中断后，用它接着跑而不是重复啃失败源
    try:
        skip_tested = int(data.get("skip_tested_days") or 0)
    except (TypeError, ValueError):
        skip_tested = 0
    r = source_verify.start(keyword=(data.get("keyword") or None),
                            uids=data.get("uids") or None,
                            limit=limit,
                            include_disabled=bool(data.get("include_disabled")),
                            offset=offset,
                            skip_verified_days=skip_days,
                            skip_tested_days=skip_tested)
    code = 202 if r.get("started") else 200
    return jsonify({"ok": True, **r}), code


@bp.route("/api/sources/verify/status")
def api_sources_verify_status():
    return jsonify({"ok": True, **source_verify.status()})


@bp.route("/api/sources/verify/results")
def api_sources_verify_results():
    return jsonify({"ok": True, **source_verify.results_payload()})


def _manga_explore_info():
    """漫画侧探索能力：遍历已注册适配器，取真正实现了 categories() 的源。"""
    from engine.manga.manager import list_adapters, get_adapter
    try:
        from server.state import _load_manga_adapters, MANGA_STATE_DIR as _sd
        _load_manga_adapters()
    except Exception:
        _sd = None
    sources = []
    for info in list_adapters():
        key = info.get("key") or ""
        try:
            ad = get_adapter(key, state_dir=_sd) if _sd else get_adapter(key)
        except Exception:
            ad = None
        if ad is None or not hasattr(ad, "categories"):
            continue
        try:
            cats = ad.categories() or []
        except Exception:
            cats = []
        if cats:
            sources.append({"key": key, "name": info.get("name") or key,
                            "categories": [{"key": c.get("key"), "name": c.get("name"),
                                            "group": c.get("group") or ""}
                                           for c in cats if isinstance(c, dict)]})
    if sources:
        return {"supported": True, "sources": sources,
                "reason": ""}
    return {"supported": False, "sources": [],
            "reason": "漫画适配器未提供排行/分类接口（需要适配器实现后才会出现在探索页）"}


@bp.route("/api/explore/sources")
def api_explore_sources():
    """提供榜单/分类的源清单。

    只列出**声明了 exploreUrl/ruleExplore** 的源——没有的就不出现在这里，
    也不拿搜索结果冒充榜单（诊断 §4）。返回里带上每个源的分类入口。
    """
    from engine import explore as _ex
    from engine.source_mgr import load_all
    allsrc = load_all() or []
    items = _ex.sources_with_explore(allsrc)
    return jsonify({
        "sources": items,
        "total_sources": len(allsrc),
        "with_explore": len(items),
        "note": ("只有声明了 exploreUrl/ruleExplore 的源才有榜单/分类；"
                 "其余源不提供，不做假榜单"),
        # 漫画侧如实说明：适配器没有排行/分类接口，因此漫画不参与探索。
        # 与其让用户纳闷"为什么漫画没有探索"，不如在接口里讲清楚（且不编造榜单）。
        # 漫画侧：按**适配器实际实现**计算（不写死）。MangaDex 已实现 categories()/browse()，
        # 因此这里会如实变成 supported=true 并列出各源与分类；没实现的源仍然不出现。
        "manga": _manga_explore_info(),
    })


@bp.route("/api/explore")
def api_explore():
    """按分类地址取一页榜单书单：/api/explore?source=<uid>&url=<分类地址>&page=1"""
    from engine import explore as _ex
    from engine.source_mgr import get_by_uid
    uid = _safe_str_arg("source", maxlen=100)
    url = request.args.get("url") or ""
    if not uid or not url:
        return _err_json("缺少 source 或 url")
    src = get_by_uid(uid)
    if not src:
        abort(404, "书源不存在")
    try:
        page = int(request.args.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    try:
        books = _ex.explore(src, url, page=page)
    except _ex.ExploreError as e:
        return _err_json(str(e), 502)
    except Exception as e:
        return _err_response(e, 500, "探索失败")
    return jsonify({"books": books, "total": len(books), "page": page,
                    "source_uid": uid, "url": url})


@bp.route("/api/net-ips")
def api_net_ips():
    """本机局域网 IPv4 列表 + mDNS 固定主机名(供页面实时显示手机访问地址;
    网络切换后 IP 变化 → 前端轮询此接口刷新; host_local 不随 DHCP 变)"""
    import re as _re
    import subprocess as _sp
    import socket as _sock
    import ipaddress as _ipa
    ips = set()
    try:
        _out = _sp.run(["ifconfig"], capture_output=True, text=True,
                       timeout=5).stdout
        for _m in _re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)", _out or ""):
            _ip = _m.group(1)
            try:
                _o = _ipa.ip_address(_ip)
                if _o.is_private and not _o.is_loopback:   # 仅局域网私网段
                    ips.add(_ip)
            except ValueError:
                pass
    except Exception:
        pass
    # mDNS 固定名(如 MacBook-Pro.local)：不随 DHCP/换网变化，手机端最稳的入口
    host_local = ""
    try:
        host_local = (_sock.gethostname() or "").strip().lower()
        if host_local and not host_local.endswith(".local"):
            host_local += ".local"
    except Exception:
        pass
    # 端口跟随实际监听（app.py 启动时把 --port 写入 PORT 环境变量），不再硬编码
    return jsonify({"ips": sorted(ips),
                    "port": int(os.environ.get("PORT", 8766)),
                    "host_local": host_local})


@bp.route("/api/diagnostics/report")
def api_diagnostics_report():
    """脱敏诊断报告（方向基线 §7.1：让用户一次导出即可定位故障分层）

    返回 {"ok": true, "text": "<纯文本报告>"}。App 侧会在这段文本前面补上
    产物基线（版本/revision/构建时间）与设备信息，再经系统文件选择器保存。
    脱敏口径见 server/diag 的模块说明与报告第 7 节。
    """
    from server import diag as _diag
    try:
        text = _diag.build_report()
    except Exception as e:
        _diag.record_error("diag.build_report", e)
        return _err_response(e, 500, "诊断报告生成失败")
    return jsonify({"ok": True, "bytes": len(text.encode("utf-8")), "text": text})


@bp.route("/api/diagnostics/latency")
def api_diagnostics_latency():
    """阅读链路分段测速（0.71.0）：让用户在**目标机**上一次拿到
    "哪一段慢、走的是哪条传输"，不必靠复述。

    参数：source（默认 jm）、comic_id（可选，省一次搜索）。
    只读、每步超时 10s、失败如实记录；不会写盘、不改缓存。
    """
    from server import diag as _diag
    src = (request.args.get("source") or "jm").strip()
    cid = (request.args.get("comic_id") or "").strip()
    try:
        data = _diag.measure_latency(src, cid)
        lines = _diag.latency_lines(src, cid, data=data)
    except Exception as e:
        _diag.record_error("diag.latency.route", e)
        return _err_response(e, 500, "测速失败")
    return jsonify({"ok": True, "data": data, "text": "\n".join(lines)})


@bp.route("/api/diagnostics/events")
def api_diagnostics_events():
    """最近失败请求/异常（界面上的"诊断信息"展开用；报告里包含同一份数据）"""
    from server import diag as _diag
    return jsonify({"ok": True, **_diag.events()})


@bp.route("/api/sources/bulk-enabled", methods=["POST"])
def api_sources_bulk_enabled():
    """批量启用/停用书源（按验证结果筛选，或直接给 uid 列表）。

    为什么需要：内置 34 个源的启用状态是用户长期点出来的，手机端只会用启用中的源；
    要"只启用验证通过的、停用失效的"若只能一个个点，等于没有这个能力。

    口径（不猜、不夸大）：
      - 只按 **已记录的** 功能验证结论筛选；没有记录的源属于 `unverified`，
        不会被"验证通过"过滤器带上（没测过 ≠ 通过）。
      - 返回 matched / changed / missing 三个真实条数，界面照实说"改了几个"。
      - 已经处于目标状态的源**不算改动**，也不重写文件。
    """
    from engine.source_mgr import (load_all, load_all_with_files,
                                   set_enabled_bulk, set_enabled_files)
    from engine import source_verify as _sv

    data = request.get_json(silent=True) or {}
    if "enabled" not in data:
        abort(400, "缺少 enabled 字段")
    enabled = bool(data.get("enabled"))
    flt = (data.get("filter") or "").strip()
    uids_arg = data.get("uids")

    allsrc = load_all() or []
    files_all = load_all_with_files()
    cur = {}                      # uid → 该源是否启用（任一文件启用即算启用）
    for s_ in allsrc:
        u = s_.get("uid") or ""
        if not u:
            continue
        cur[u] = cur.get(u, False) or bool(s_.get("enabled", True))

    def uid_of(fn, d):
        return d.get("uid") or fn[:-5]

    matched_uids = 0
    matched_entries = 0
    missing = []
    note = ""
    if isinstance(uids_arg, list) and uids_arg:
        targets = [str(u) for u in uids_arg if str(u).strip()]
        res = set_enabled_bulk(targets, enabled)
        matched_uids = res["matched"]
        missing = res["missing"]
        _hit_uids = set(targets) - set(missing)
        matched_entries = sum(1 for _fn, d in files_all if uid_of(_fn, d) in _hit_uids)
    elif isinstance(uids_arg, list):
        return jsonify({"ok": True, "enabled": enabled, "filter": "",
                        "total": len(cur), "total_entries": len(files_all),
                        "matched": 0, "matched_entries": 0, "changed": 0,
                        "missing": [], "enabled_now": sum(1 for v in cur.values() if v),
                        "enabled_entries": sum(1 for _f, d in files_all
                                               if d.get("enabled", True)),
                        "note": "uids 为空列表，未改动任何书源"})
    elif flt in ("enabled", "disabled", "all"):
        # 按**当前状态**筛选（以及 all）：文件级操作——恒等调用必须零改动，
        # 也不能顺手牵动同 uid 的其它文件
        want_now = (flt == "enabled")
        files = [fn for fn, d in files_all
                 if flt == "all" or bool(d.get("enabled", True)) == want_now]
        res = set_enabled_files(files, enabled)
        matched_entries = len(files)
        matched_uids = len({uid_of(fn, d) for fn, d in files_all if fn in set(files)})
    elif flt:
        status_of = {}
        try:
            for it in (_sv.results_payload().get("items") or []):
                status_of[it.get("uid")] = it.get("status") or ""
        except Exception:
            status_of = {}
        if flt in ("verified", "partial", "failed", "unsupported", "skipped"):
            targets = [u for u in cur if status_of.get(u) == flt]
        elif flt == "unverified":
            targets = [u for u in cur if u not in status_of]
        else:
            abort(400, f"未知 filter：{flt}")
        res = set_enabled_bulk(targets, enabled)
        matched_uids = res["matched"]
        missing = res["missing"]
        _hit = set(targets)
        matched_entries = sum(1 for _fn, d in files_all if uid_of(_fn, d) in _hit)
        if not targets:
            note = (f"没有匹配的书源（filter={flt}）：未改动任何文件；"
                    "该状态当前没有任何记录（先在书源管理里跑逐源功能验证）")
    else:
        abort(400, "需要 filter 或 uids 之一")

    res_changed = res.get("changed", 0)
    # 回读真实状态（不按"应该变成什么"推算）：界面显示的启用数必须来自磁盘。
    # 两种口径都要给：**唯一源数**（同一 uid 的重复文件算一个）与**文件数**。
    # 只给一种必然有地方对不上（实测：设备上 34 个文件 = 30 个唯一 uid）。
    after = load_all() or []
    after_files = load_all_with_files()
    enabled_now = len({s_.get("uid") for s_ in after if s_.get("enabled", True)})
    enabled_entries = sum(1 for _f, d in after_files if d.get("enabled", True))
    if not note:
        if matched_uids == 0 and matched_entries == 0:
            note = "没有匹配的书源：未改动任何文件"
        elif res_changed == 0:
            note = (f"匹配 {matched_uids} 个源（{matched_entries} 个文件），"
                    "但都已经处于目标状态，无需改动")
    return jsonify({"ok": True, "enabled": enabled, "filter": flt,
                    "total": len(cur), "total_entries": len(after_files),
                    "matched": matched_uids, "matched_entries": matched_entries,
                    "changed": res_changed, "missing": missing[:20],
                    "enabled_now": enabled_now,
                    "enabled_entries": enabled_entries,
                    "note": note})


@bp.route("/api/sources/<uid>/toggle", methods=["POST"])
def api_source_toggle(uid):
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    try:
        ok = set_enabled(uid, enabled)
    except ValueError:
        abort(400, "书源 UID 非法")
    if not ok:
        abort(404, "书源不存在")
    return jsonify({"ok": True, "enabled": enabled})


@bp.route("/api/sources/<uid>", methods=["DELETE"])
def api_source_delete(uid):
    try:
        ok = delete_source(uid)
    except ValueError:
        abort(400, "书源 UID 非法")
    if not ok:
        abort(404, "书源不存在")
    return jsonify({"ok": True})


@bp.route("/api/hot-novels")
def api_hot_novels():
    """热门推荐（发现能力）：从可用源的首页热门/热门书兜底取书"""
    from engine.adapters.quanben_io import HOT_SLUGS as _HS
    books = []
    for _h in _HS[:12]:
        _name, _slug = _h[0], _h[1]   # (name, slug[, author]) 兼容三元组
        books.append({"name": _name, "author": "", "cover": "",
                      "book_url": f"https://www.quanben.io/n/{_slug}/",
                      "source_uid": "quanben.io",
                      "source_name": "全本小说网"})
    return jsonify({"books": books})


@bp.route("/api/search")
def api_search():
    q = _safe_str_arg("q")
    uid = _safe_str_arg("source", maxlen=100)
    if not q:
        return _err_json("缺少关键词")
    # 繁体查询 → 简体（源站多为简体内容，避免"劍來"搜不到）
    if q and q != _norm(q):
        q = _norm(q)
    sort_mode = request.args.get("sort") or "default"
    search_type = request.args.get("type") or "all"
    # 搜索级缓存：同一关键词+参数 60s 内直接返回（显著提速重复搜索）
    _cache_key = (q, uid, sort_mode, search_type)
    with _toc_lock:
        _cached = _search_cache.get(_cache_key)
    # R52: 空结果缓存视为无效(源站抽风/全源失败时曾把空结果写缓存，
    # 导致用户"一直搜索无结果")——空缓存直接忽略,走实时搜索
    if _cached and _cache_ok(_cached) and _cached["payload"].get("groups"):
        return jsonify(_cached["payload"])
    # source 参数支持：精确 uid / 书源 URL 包含 / 书源名包含
    if uid:
        src = find_source(uid)
        if not src:
            return jsonify({"books": [], "error": f"书源不存在: {uid}"}), 404
        sources = [src]
    else:
        sources = load_enabled()
    sources = [s for s in sources if s and s.get("searchUrl")]
    if not sources:
        return jsonify({"books": [], "error": "无可用的可搜索书源"}), 200
    # 慢源记忆：最近失败/超时的书源跳过本次搜索（后台仍会重试），
    # 避免个别慢源拖垮整体搜索响应
    now = time.time()
    skipped = []
    fast_sources = []
    for s in sources:
        uid = s.get("uid", "")
        rec = _slow_sources.get(uid)
        if rec and now - rec["ts"] < SLOW_COOLDOWN:
            skipped.append(s)
        else:
            fast_sources.append(s)
    if skipped:
        print(f"[search] 跳过慢源 {len(skipped)}: "
              + ", ".join(s.get('bookSourceName','')[:10] for s in skipped[:8]), flush=True)
    # 并发搜索 + 总时限：超时返回已完成部分，慢源跳过不阻塞
    from concurrent.futures import ThreadPoolExecutor, wait
    all_books = []
    def _search_one(s):
        return _search_worker.search_one(s, q)
    ex = ThreadPoolExecutor(max_workers=SEARCH_MAX_WORKERS)
    futs = {}
    try:
        futs = {ex.submit(_search_one, s): s for s in fast_sources}
        # P1-3: 单源时限由 search_one 内的业务 deadline 熔断保证
        # （SEARCH_SOURCE_TIMEOUT 到点中止后续网络操作），
        # 外层只留调度余量；健康源 1-3s 返回，整体搜索不再等死源重试链
        done, pending = wait(futs, timeout=SEARCH_SOURCE_TIMEOUT + 1)
        for fut in done:
            all_books.extend(fut.result())
        for fut in pending:
            fut.cancel()
            # deadline 无法覆盖的卡死路径（如 DNS 阻塞）：在此补记慢源，
            # 保持旧版"内层超时即记慢源"语义
            _slow_tracker.mark_slow(futs[fut].get("uid", ""),
                                    f"超时{SEARCH_SOURCE_TIMEOUT}s")
    finally:
        # B01: 退出（含超时放弃/异常路径）时取消未启动的 future——
        # shutdown(wait=False) 不回收队列里未开始的任务，它们仍会在后台
        # 执行网络调用；运行中的任务靠 search_one 内 deadline 协作中止
        for f in futs:
            if not f.done():
                f.cancel()
        # 不等待慢任务：让其在后台自然结束，保证 API 按时返回
        ex.shutdown(wait=False)
    # ── 过滤/打分/分组/排序：统一走 _build_groups（与流式搜索同一实现）──
    # R47 消除内联副本（约 140 行）：两处逻辑已漂移——内联版组内不保留最高分，
    # 且响应快照在"组内源排序"之前序列化，首次响应与缓存响应的源顺序不一致
    for b in all_books:
        _clean_book(b)
    glist = _build_groups(all_books, q, search_type, sort_mode)

    # ── 作者补齐（R22）：适配器搜索未解析作者的组，响应前并发 fast 补作者
    # （预算 1.5s，只补排序靠前 20 组——用户主要看前几屏；其余交给后台增强）
    def _fill_authors_sync(_glist, budget=1.5):
        _empty = [g for g in _glist
                  if not (g.get('author') and g['author'] != '佚名')][:20]
        if not _empty:
            return
        from concurrent.futures import ThreadPoolExecutor as _TPE, wait as _wait
        from engine.search_service import enhance_group
        # R47: 预载源映射，避免每组 find_source 全量读 sources 目录
        from engine.source_mgr import load_all as _load_all
        try:
            _srcmap = {s.get('uid', ''): s for s in _load_all()}
        except Exception:
            _srcmap = None
        def _one(g):
            try:
                enhance_group(g, _toc_cache, TOC_CACHE_TTL, lock=_toc_lock,
                              srcmap=_srcmap)
            except Exception:
                pass
        _ex = _TPE(max_workers=8)
        try:
            _done, _ = _wait([_ex.submit(_one, g) for g in _empty],
                             timeout=budget)
        finally:
            _ex.shutdown(wait=False)
    _fill_authors_sync(glist)

    # ── 详情增强：并发对全部组做 fast 详情（章节数/最新章节/更新/字数）──
    # R19: 后台异步增强——增强只补元数据，不应阻塞响应（此前 7s 搜索 + 8s 增强
    # 同步等待 = 非流式最坏 15s）。先序列化响应快照（避免线程并发改 glist），
    # 后台线程完成后更新搜索缓存（1h 内下次请求秒回增强版；App 也按需自行拉 toc）
    # P0-4: 响应仍携带 books 原始列表（tests/test_api.py::test_search_basic
    # 断言 books 非空，属既有响应契约），但不再写入搜索缓存——见下方缓存写入
    # 2026-09-18（真断网归因）：阻塞式响应也要带 `network_down`——客户端在
    # "无结果"时据此显示"本机当前没有网络"，而不是让用户以为所有书源都坏了
    # （漫画侧阻塞搜索一直带；小说侧此前只有**流式**路径带，
    #  于是流式不可用走回落时，断网被显示成"没有结果"）。
    # 判定复用 engine/neterr 的设备级证据：客户端信号 + 直连探测 + 逐源失败文字，
    # 与流式路径同一判据（`round_is_offline`）。
    from engine import neterr as _ne
    try:
        from server.state import device_offline_hint as _dev_hint
        _dev_off = _dev_hint()
    except Exception:                                            # noqa: BLE001
        _dev_off = None
    # `round_is_offline` 的第二个参数只用来回答"这一轮确实一无所获"——
    # 逐源失败文字**不能**当整机断网证据（见 engine/neterr.py）。
    # 这里用"到点仍未返回的源名"如实填充：pending 为空且无结果 = 源站都答了、
    # 只是没这本 → 不该说成断网。
    # pending 是**未返回的 future 集合**（futs: future → 源配置），按映射取源名，
    # 别拿 future 的 repr 充数（那样文案没有信息量）。
    _errs = [str((futs.get(_f) or {}).get("bookSourceName")
                 or (futs.get(_f) or {}).get("uid") or "源")
             for _f in pending]
    # 没有逐源失败信息时传 None（而不是空列表）：`round_is_offline` 里
    # "error_texts 非 None 且为空"会**先于**设备信号判定为"这轮不是断网"——
    # 而刚断网时源站是**快速失败**（pending 为空、失败文案也没采集），
    # 传空列表会把"本机没网"的证据（客户端 X-Device-Net / 直连探测）挡掉。
    # 传 None = 如实说"没有逐源证据，请按设备级证据判定"。
    _fail_texts = _errs if _errs else None
    payload = {"groups": glist, "total": len(glist),
               "books": all_books,
               "partial": len(pending) > 0,
               "timed_out_sources": len(pending),
               "network_down": _ne.round_is_offline(
                   bool(glist), _fail_texts, device_offline=_dev_off)}
    _resp_body = json.dumps(payload, ensure_ascii=False)
    # 组内源排序已由 _build_groups 完成（响应快照与缓存写入一致）

    def _enhance_bg(_glist, _cache_key):
        try:
            from engine.search_service import run_enhance
            run_enhance(_glist, _toc_cache, TOC_CACHE_TTL, lock=_toc_lock)
            for _g in _glist:
                _g['sources'].sort(key=lambda s: -(s.get('chapter_count') or 0))
            with _toc_lock:
                _c = _search_cache.get(_cache_key)
                if _c:
                    _c["payload"]["groups"] = _glist
                    _c["ts"] = time.time()
            _search_cache_save()
            print(f"[search-enhance] {_cache_key[0]}: 后台增强完成 "
                  f"({len(_glist)} 组, 缓存更新={_c is not None})", flush=True)
        except Exception as _e:
            print(f"[search-enhance] {_cache_key[0]}: 后台增强失败: "
                  f"{type(_e).__name__}: {_e}", flush=True)
    threading.Thread(target=_enhance_bg, args=(glist, _cache_key),
                     daemon=True).start()

    # 写入搜索缓存（限制容量，落盘；含未增强快照，后台增强完成后覆写）
    # R52: 空结果不写缓存——一次全源失败/抽风的空结果不应让后续搜索
    # "一直无结果"(命中缓存秒回空);清掉旧空缓存条目
    # P0-4: 缓存 payload 去掉 books 原始全量列表（~300 条/次，是
    # search_cache.json 2.1MB 膨胀的主因），只存 groups + total 等轻量字段。
    # 前端 index.html 只用 books.length 取总数（已改为按 groups 源数计算），
    # 安卓 NovelSearchResponse 模型本无 books 字段——无消费方，纯浪费
    with _toc_lock:
        if payload.get("groups"):
            _search_cache[_cache_key] = {
                "ts": time.time(),
                "ver": SEARCH_CACHE_VERSION,
                "payload": {"groups": payload["groups"],
                            "total": payload["total"],
                            "partial": payload["partial"],
                            "timed_out_sources": payload["timed_out_sources"]}}
        else:
            _search_cache.pop(_cache_key, None)
        if len(_search_cache) > 500:
            for k in sorted(_search_cache,
                            key=lambda x: _search_cache[x]["ts"])[:250]:
                _search_cache.pop(k, None)
    _search_cache_save()
    return Response(_resp_body, mimetype="application/json",
                    headers={"Cache-Control": "no-store"})


def _group_fp(g):
    """A03：分组内容指纹——判断已推送分组是否发生变化（晚到来源补齐 /
    元数据变化 / 排序相关字段变化）。指纹变化 → 以 updates 语义重发该组，
    前端原地刷新卡片；不变 → 不重发（增量推送的带宽优化保持有效）。"""
    return (
        g.get('name') or '', g.get('author') or '',
        bool(g.get('intro')), bool(g.get('cover')),
        g.get('score') or 0, g.get('update_ts') or 0, g.get('word_num') or 0,
        tuple((s.get('source_uid') or '', s.get('book_url') or '',
               s.get('chapter_count') or 0)
              for s in (g.get('sources') or ())),
    )


@bp.route("/api/search/stream")
def api_search_stream():
    """流式搜索（SSE）：每个书源完成即推送增量结果，快源先出。
    响应格式：text/event-stream，事件 data: JSON：
      {groups, updates, done, total, elapsed, finished}
    A03 更新协议：
      - groups   增量阶段 = 新出现的分组（key=name|author 首次推送）；
      - updates  增量阶段 = 已推送分组的内容更新（同书晚到来源补齐 /
                 元数据变化），前端原地刷新卡片、不移动位置；
      - finished=True 时 groups 为全量最终快照，前端据此校准来源、
        元数据与排序（与缓存命中秒回的 groups 完全一致）。
    """
    q = _safe_str_arg("q")
    uid = _safe_str_arg("source", maxlen=100)
    if not q:
        return _err_json("缺少关键词")
    # 繁体查询 → 简体（源站多为简体内容）
    if q and q != _norm(q):
        q = _norm(q)
    sort_mode = request.args.get("sort") or "default"
    search_type = request.args.get("type") or "all"


    if uid:
        src = find_source(uid)
        if not src:
            return jsonify({"books": [], "error": f"书源不存在: {uid}"}), 404
        sources = [src]
    else:
        sources = load_enabled()
    sources = [s for s in sources if s and s.get("searchUrl")]

    # 搜索缓存命中 → 立即推送全量结果（瞬时秒回）
    _cache_key = (q, uid, sort_mode, search_type)
    with _toc_lock:
        _cached = _search_cache.get(_cache_key)
    # R52: 空结果缓存视为无效——源站抽风把空结果写缓存后会导致
    # "一直搜索无结果",空缓存直接忽略走实时搜索
    # R47: 与 api_search 对齐走 _cache_ok（版本+过期统一校验），
    # 不再单独判断 TTL（旧版无 ver 的脏缓存一律不命中）
    if _cached and _cache_ok(_cached) and _cached["payload"].get("groups"):
        _payload = _cached["payload"]
        def _cached_gen():
            # 字段语义必须与**实时路径**一致（0.74.19 修不实显示）：
            #   · done  = 已返回的源数（实时路径是 1..N）→ 缓存命中等价于
            #     "全部源都已返回"（结果本来就是那一次全量搜索的快照）；
            #   · timed_out_sources 是**另一个**含义（这次搜索里超时的源数），
            #     旧实现把它塞进 done，于是 App 的进度行在缓存命中时显示
            #     "已返回 0/18 个源"——结果明明全在，却像什么都没返回。
            yield "data: " + json.dumps({
                "groups": _payload.get("groups", []),
                "done": len(sources),
                "total": len(sources),
                "timed_out_sources": _payload.get("timed_out_sources", 0),
                "partial": bool(_payload.get("partial")),
                "elapsed": 0,
                "finished": True,
                "books": _payload.get("books", []),
                "cached": True,
            }, ensure_ascii=False) + "\n\n"
        return Response(_cached_gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    # 慢源跳过（与 api_search 一致）
    now = time.time()
    fast_sources = []
    for s in sources:
        rec = _slow_sources.get(s.get("uid", ""))
        if not (rec and now - rec["ts"] < SLOW_COOLDOWN):
            fast_sources.append(s)

    from server.state import device_offline_hint
    _dev_off = device_offline_hint()
    from concurrent.futures import (ThreadPoolExecutor, as_completed, wait as _cf_wait,
                                    FIRST_COMPLETED as _CF_FIRST,
                                    TimeoutError as _CfTimeout)
    def _search_one(s):
        books = _search_worker.search_one(s, q, tag="search-stream")
        return [b for b in books if b.get('name')]

    def _gen():
        all_books = []
        total = len(fast_sources)
        done_count = 0
        t0 = time.time()
        # P2-3: 会话级分组计算缓存——每完成一个源全量重建分组时，
        # 老书的 OpenCC/正则/打分不再重复计算（贯穿增量重建与最终快照）
        _gcache = {}
        # 快源优先：按历史延迟升序发射（无记录源排中间，保持响应速度）
        _, _lat = _slow_tracker.snapshot()
        fast_sources.sort(key=lambda s: _lat.get(s.get("uid", ""), 5.0))
        # 精确命中提前停止：书名精确==关键词的书达到阈值即取消剩余慢源
        exact_threshold = STREAM_STOP_EXACT
        # A03: key -> 内容指纹。key 首次出现 → groups（新增语义）；
        # 已出现但指纹变化 → updates（更新语义：同书晚到来源补齐等），
        # 前端原地刷新卡片；指纹不变 → 不重发（增量推送保持省带宽）。
        sent_state = {}
        # C06: 逐源失败/超时原因（源名 -> 原因），finished 事件随 errors 字段下发，
        # 前端据此区分"源失败"与"真正无结果"（源失败不得描述为"搜不到书"）。
        # 判据复用慢源台账：search_one 内部对异常/超时记 mark_slow(reason)，
        # 空结果记 "空结果 Xs"（不算失败）；ts >= t0 保证只认本轮记录。
        _errors = {}
        # 真断网：本机连不出去时不再干等源站期限（判据见 engine/neterr.py）
        _offline_hit = False
        _patience_stop = False
        _ne_probe = {"done": False, "down": False}

        def _probe_down():
            if not _ne_probe["done"]:
                from engine import neterr as _ne
                _ne_probe["down"] = _ne.local_network_down()
                _ne_probe["done"] = True
            return _ne_probe["down"]

        def _offline_evidence():
            """设备级证据：客户端信号或直连探测（逐源失败文字不算，见 engine/neterr.py）"""
            return bool(_dev_off) or _probe_down()

        def _mark_patience_pending():
            """耐心上限提前收尾：未返回的源如实写成"仍在查询"，不假装超时/没结果"""
            nonlocal _patience_stop
            _patience_stop = True
            for _f, _s in futs.items():
                if not _f.done():
                    _errors[_s.get("bookSourceName") or _s.get("uid", "?")] = \
                        "仍在查询（已先显示其它源的结果，稍后重试可补上）"
                    _f.cancel()

        def _mark_offline_pending():
            """断网提前收尾：未返回的源如实写成"本机网络不可达"，不假装超时"""
            nonlocal _offline_hit
            _offline_hit = True
            from engine import neterr as _ne
            for _f, _s in futs.items():
                if not _f.done():
                    _f.cancel()
                    _errors[_s.get("bookSourceName") or _s.get("uid", "?")] = (
                        f"搜索失败（{_ne.REASON_UNREACHABLE}），"
                        f"本机没有可用网络，已提前结束等待")

        def _iter_done(futs):
            """按完成顺序产出 future（替代 as_completed）。

            为什么要自己写：as_completed 只在**有源完成**时才回到调用方，
            一个卡死的源会让"本机没网"这个结论迟到到它超时为止（实测 3s）；
            这里每 0.5s 回到调用方一次，本机确实没有路由时立刻收尾。
            总时限到 → 抛 _CfTimeout，由外层既有逻辑取消未启动任务并记慢源。
            """
            _pending = set(futs)
            _dl = t0 + SEARCH_SOURCE_TIMEOUT + 2
            while _pending:
                if time.time() >= _dl:
                    raise _CfTimeout()
                _finished, _pending = _cf_wait(
                    _pending, timeout=0.5, return_when=_CF_FIRST)
                for _f in _finished:
                    yield _f
                # 已有结果 → 不再为一个慢源把用户晾在单源时限上（耐心上限）
                if all_books and time.time() - t0 >= NOVEL_PATIENCE:
                    _mark_patience_pending()
                    print(f"[search-stream] 已有结果，耐心上限到，提前收尾"
                          f"（{done_count}/{total}源）", flush=True)
                    return
                if (not _finished and not all_books
                        and time.time() - t0 >= NOVEL_OFFLINE_GRACE
                        and _offline_evidence()):
                    _mark_offline_pending()
                    print(f"[search-stream] 本机无网络，提前结束（{done_count}/{total}源）",
                          flush=True)
                    return

        def _note_src_error(s):
            rec = _slow_tracker.snapshot()[0].get(s.get("uid", ""))
            if not rec or rec.get("ts", 0) < t0:
                return
            reason = rec.get("reason") or ""
            if reason.startswith("空结果"):
                return   # 真空结果，非源失败
            _errors[s.get("bookSourceName") or s.get("uid", "?")] = reason
        ex = ThreadPoolExecutor(max_workers=SEARCH_MAX_WORKERS)
        futs = {}
        try:
            futs = {ex.submit(_search_one, s): s for s in fast_sources}
            try:
                # P1-3: 单源时限由 search_one 内业务 deadline 熔断；
                # as_completed 总时限兜底 deadline 覆盖不到的卡死路径
                # （如 DNS 阻塞），保证 finished 事件按时发出
                for fut in _iter_done(futs):
                    books = fut.result()
                    done_count += 1
                    if not books:
                        _note_src_error(futs[fut])   # C06: 区分源失败与真空结果
                    new_groups = []
                    upd_groups = []
                    if books:
                        for b in books:
                            _clean_book(b)
                        all_books.extend(books)
                        # 增量重建分组（只对新结果全量重建，量小可接受）
                        glist = _build_groups(all_books, q, search_type,
                                              sort_mode, _cache=_gcache)
                        # A03: 新增 → groups；内容变化 → updates（晚到来源
                        # 补齐/元数据变化）；无变化不重发（百级书源时显著省带宽）
                        for g in glist[:60]:
                            k = g.get('name', '') + '|' + (g.get('author') or '')
                            fp = _group_fp(g)
                            old = sent_state.get(k)
                            if old is None:
                                sent_state[k] = fp
                                new_groups.append(g)
                            elif old != fp:
                                sent_state[k] = fp
                                upd_groups.append(g)
                        exact_cnt = sum(1 for g in glist if g.get('name') == q.strip())
                        # 精确命中足够 → 提前结束（取消剩余慢源）
                        if exact_cnt >= exact_threshold:
                            if new_groups or upd_groups:
                                yield "data: " + json.dumps({
                                    "groups": new_groups,
                                    "updates": upd_groups,
                                    "done": done_count,
                                    "total": total,
                                    "elapsed": round(time.time() - t0, 1),
                                    "finished": False,
                                }, ensure_ascii=False) + "\n\n"
                            for f in futs:
                                f.cancel()
                            print(f"[search-stream] 精确命中{exact_cnt}个，提前结束（{done_count}/{total}源）",
                                  flush=True)
                            break
                    # 每个源完成都发事件（无新分组时仅进度数字，payload 极小）
                    if books or done_count % 10 == 0:
                        yield "data: " + json.dumps({
                            "groups": new_groups,
                            "updates": upd_groups,
                            "done": done_count,
                            "total": total,
                            "elapsed": round(time.time() - t0, 1),
                            "finished": False,
                        }, ensure_ascii=False) + "\n\n"
            except _CfTimeout:
                # 个别源卡死在 deadline 覆盖不到的路径：补记慢源后继续
                # 走最终快照，流按时收尾
                for f, s in futs.items():
                    if not f.done():
                        f.cancel()
                        _slow_tracker.mark_slow(
                            s.get("uid", ""), f"超时{SEARCH_SOURCE_TIMEOUT}s")
                        _errors[s.get("bookSourceName") or s.get("uid", "?")] = \
                            f"超时{SEARCH_SOURCE_TIMEOUT}s"   # C06: 计入源失败
                print(f"[search-stream] 总时限到，{done_count}/{total} 源完成，"
                      f"其余按慢源处理", flush=True)
        finally:
            # B01: 客户端断连（GeneratorExit）/精确命中提前结束/总时限放弃
            # ——取消未启动的 future，未开始的搜索不再执行网络调用；
            # 运行中的任务保持协作取消（search_one 内业务 deadline 到点熔断）。
            # shutdown(wait=False) 模式保留：只保证未启动任务不执行。
            for f in futs:
                if not f.done():
                    f.cancel()
            ex.shutdown(wait=False)
        # 最终快照（全量，复用同一会话缓存）
        glist = _build_groups(all_books, q, search_type, sort_mode,
                              _cache=_gcache)
        # 写入搜索缓存（供后续秒回）
        # R52: 空结果不写缓存(防"一次抽风→一直无结果");顺带清旧空缓存
        try:
            with _toc_lock:
                if glist:
                    # R47 修复：此前漏写 "ver"——_cache_ok 的版本校验把流式
                    # 写入的缓存一律判失效，SEARCH_CACHE_VERSION 治理形同虚设
                    # P0-4: 缓存 payload 不再存 books 原始列表（缓存膨胀主因，
                    # 前端/安卓均无消费），只存 groups + total
                    # 部分结果（耐心上限/断网提前收尾，或仍有源没回来）只**短暂**缓存：
                    # state._cache_ok 见到 partial=True 就用短 TTL，避免把"还有源没跑"的
                    # 结果当成一小时的完整结果（与漫画端 _MANGA_PARTIAL_TTL 同一口径）
                    _partial = bool(_patience_stop or _offline_hit) or (
                        not _patience_stop and not _offline_hit and bool(_errors))
                    _search_cache[_cache_key] = {
                        "ts": time.time(),
                        "ver": SEARCH_CACHE_VERSION,
                        "payload": {"groups": glist,
                                    "total": len(glist),
                                    "partial": _partial,
                                    "timed_out_sources": len(_errors)}}
                else:
                    _search_cache.pop(_cache_key, None)
                if len(_search_cache) > 500:
                    for k in sorted(_search_cache,
                                    key=lambda x: _search_cache[x]["ts"])[:250]:
                        _search_cache.pop(k, None)
            _search_cache_save()
        except Exception:
            pass
        # P0-4: finished 事件不再携带 books 原始全量列表（数百条，纯带宽浪费）;
        # 前端已改为按 groups 内 sources 数计算"源结果数"
        # 2026-09-15（真断网）：整轮无结果且本机连不出去时，前端必须能说
        # "本机没网"，而不是让用户以为所有书源都坏了（见 engine/neterr.py）
        from engine import neterr as _ne
        _net_down = _offline_hit or _ne.round_is_offline(
            bool(glist), list(_errors.values()), device_offline=_dev_off)
        yield "data: " + json.dumps({
            "groups": glist,
            "done": done_count,
            "total": total,
            "elapsed": round(time.time() - t0, 1),
            "finished": True,
            # C06: 源名 -> 失败/超时原因；空 dict = 无源失败（前端区分四态）
            "errors": _errors,
            "network_down": _net_down,
        }, ensure_ascii=False) + "\n\n"

    return Response(_gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ══════════════════════════════════════════════
#  任务 API
# ══════════════════════════════════════════════
@bp.route("/api/tasks", methods=["GET"])
def api_tasks():
    with _lock:
        snaps = [_task_snapshot(tid) for tid in _tasks]
    snaps.sort(key=lambda x: x["created_at"] or "", reverse=True)
    # 合并漫画下载任务（下载管理器）
    for key, job in _manga_dl.all_tasks().items():
            snaps.append({
                "id": "manga_" + key, "type": "manga", "manga_key": key,
                "source_uid": job.get("source", ""), "book_url": job.get("comic_id", ""),
                "book_key": job.get("comic_id", ""),
                "title": job.get("title", "漫画下载"),
                "status": job.get("status", "idle"),
                "created_at": job.get("started_at") and time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.localtime(job.get("started_at"))),
                "finished_at": job.get("finished_at") and time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.localtime(job.get("finished_at"))),
                "progress": {
                    "status": job.get("status"), "done": job.get("done", 0),
                    "total": job.get("total", 0),
                    "current": job.get("current", ""),
                    "speed": job.get("speed", 0), "eta": job.get("eta", 0),
                    "images_done": job.get("images_done", 0),
                    "images_total": job.get("images_total", 0),
                },
                "error": job.get("error"), "running": job.get("status") == "running",
                # P1-1: 漫画下载的停止原因/断点（与小说任务同字段，界面同一套渲染）
                "stop_kind": job.get("stop_kind", ""),
                "stop_reason": job.get("stop_reason", ""),
                # cancel = 用户点了取消但 worker 还没到检查点，同样可继续
                "resumable": job.get("status") in ("paused", "stopped", "error", "cancel"),
                "checkpoint": {
                    "done": int(job.get("done") or 0),
                    "total": int(job.get("total") or 0),
                    "failed": int(job.get("failed_chapters") or 0),
                },
                "stopped_at": "",
            })
    return jsonify({"tasks": snaps})


@bp.route("/api/tasks", methods=["POST"])
def api_task_create():
    data = request.get_json(silent=True) or {}
    source_uid = data.get("source_uid") or data.get("source")
    book_url = (data.get("book_url") or data.get("url") or "").strip()
    if not source_uid or not book_url:
        return _err_json("缺少书源或书籍地址")
    # R29(技术评审4.2): SSRF 防护——仅允许公网 http/https 目标
    try:
        book_url = _safe_target_url(book_url, "书籍地址")
    except ValueError as _e:
        return _err_json(str(_e))
    # R53: 多源混合下载已取消——忽略旧客户端可能携带的备源字段
    tid, err, running_id = _create_task(source_uid, book_url)
    if err:
        return jsonify({"error": err, "task_id": running_id}), 409
    return jsonify({"id": tid, "status": ST_RUNNING}), 202


@bp.route("/api/tasks/<task_id>")
def api_task_detail(task_id):
    snap = _task_snapshot(task_id)
    if snap is None:
        abort(404, "任务不存在")
    return jsonify(snap)


@bp.route("/api/tasks/<task_id>/pause", methods=["POST"])
def api_task_pause(task_id):
    # 共享 dict 的修改与 _run_task 收尾读 pause_requested 同在 _lock 下，
    # 避免"已请求暂停却被判为 DONE"的交错；磁盘落盘仍在锁外
    with _lock:
        ctl = _crawlers.get(task_id)
        t = _tasks.get(task_id)
        if not ctl or not t:
            return _err_json("任务不在运行中")
        ctl["stop"]["v"] = True
        t["pause_requested"] = True
        # P1-1: 请求当下就把原因写下来（用户可能马上离开界面/被系统杀掉），
        # 线程收尾时会把它替换为终态文案
        _set_stop(t, STOP_KIND_USER_PAUSE, "已请求暂停，当前章节完成后生效")
        _log(t, "已请求暂停（当前章节完成后暂停）…")
    _save_task(t)
    return jsonify({"ok": True})


@bp.route("/api/tasks/<task_id>/stop", methods=["POST"])
def api_task_stop(task_id):
    if task_id.startswith("manga_"):
        manga_key = task_id[len("manga_"):]
        _manga_dl.cancel(manga_key)
        return jsonify({"ok": True, "stopped": True})
    with _lock:
        ctl = _crawlers.get(task_id)
        t = _tasks.get(task_id)
        if not ctl or not t:
            return _err_json("任务不在运行中")
        ctl["stop"]["v"] = True
        t["pause_requested"] = False
        _set_stop(t, STOP_KIND_USER_STOP, "已请求停止，当前章节完成后生效")
        _log(t, "已请求停止…")
    _save_task(t)
    return jsonify({"ok": True})


@bp.route("/api/tasks/<task_id>/resume", methods=["POST"])
def api_task_resume(task_id):
    tid, err = _launch_task(task_id)
    if err:
        return _err_json(err, 409)
    return jsonify({"id": tid, "status": ST_RUNNING}), 202


@bp.route("/api/tasks/<task_id>", methods=["DELETE"])
def api_task_delete(task_id):
    if task_id.startswith("manga_"):
        manga_key = task_id[len("manga_"):]
        _manga_dl.delete(manga_key)
        return jsonify({"ok": True})
    # 纵深防御：task_id 与 book_key 等路径段同样净化，并用 realpath
    # 校验目标仍在 TASKS_DIR 内，不再单靠 Flask 路由转换器兜底
    task_id = _safe_seg(task_id, "任务")
    with _lock:
        ctl = _crawlers.pop(task_id, None)
        t = _tasks.pop(task_id, None)
    if ctl:
        ctl["stop"]["v"] = True
    p = os.path.join(TASKS_DIR, task_id + ".json")
    _root = os.path.realpath(TASKS_DIR)
    if os.path.realpath(p).startswith(_root + os.sep) and os.path.exists(p):
        os.remove(p)
    if t is None:
        abort(404, "任务不存在")
    return jsonify({"ok": True})


@bp.route("/api/cache/clean", methods=["POST"])
def api_cache_clean():
    """手动清理各类缓存。scope 可选：'cache'（默认，保护已下载）/ 'all'（额外清 trash）。"""
    data = request.get_json(silent=True) or {}
    scope = data.get("scope") or "cache"
    freed, cleaned = _clean_caches(scope)
    return jsonify({"ok": True, "freed": freed,
                    "cleaned": [{"name": n, "size": s} for n, s in cleaned]})


@bp.route("/api/books")
def api_books():
    return jsonify({"books": _scan_books()})


def _failed_live(book_dir, failed):
    """真正"还没拿到正文"的失败章节（本机已有非空缓存的不算失败）。

    判据：`cache_key_of(url) + ".cache"` 存在且**非空** —— 与下载路径写缓存的口径
    一致（`Crawler._save_cache` 非空才写、`_has_cache` 只看存在）。只读判断、不改盘。
    """
    from engine.app_utils import cache_key_of
    out = {}
    for url, why in (failed or {}).items():
        try:
            p = os.path.join(book_dir, cache_key_of(url) + ".cache")
            if os.path.exists(p) and os.path.getsize(p) > 0:
                continue                     # 本机已有正文 → 不算失败
        except OSError:
            pass
        out[url] = why
    return out


@bp.route("/api/books/<book_key>")
def api_book_detail(book_key):

    book_key = _safe_seg(book_key, "书籍")
    d = os.path.join(BOOKS_DIR, book_key)
    if not os.path.isdir(d):
        abort(404, "书籍不存在")
    state = load_book_state(os.path.join(d, "_state.json"))
    if not state:
        abort(404, "书籍数据不完整")
    chapters = state.get("chapters", [])
    completed = set(state.get("completed", []))
    failed = state.get("failed", {}) or {}
    info = state.get("book") or {}
    cache_map = {f[:-6]: os.path.join(d, f)
                 for f in os.listdir(d) if f.endswith(".cache")}
    # 0.55.0: "已下载"只看**磁盘上真的有缓存**（与 _scan_books 同口径）。
    # 此前把 state 的 completed 也算进来，于是"缓存被清理/书目录被任务重建"
    # 的书会显示"已下载"而点开正文是空的；completed 只作诊断字段暴露。
    has_cache = lambda c: _cache_key(c.get("url", "")) in cache_map  # noqa: E731
    done = sum(1 for c in chapters if has_cache(c))
    # 诊断：state 说有、磁盘上没有的章节数（>0 说明历史进度与缓存不一致）
    stale = sum(1 for c in chapters
                if c.get("url") in completed and not has_cache(c))
    # R49: 多源补章信息（_fallback.json：备源列表 + 已补章来源映射）
    _fb = load_json(os.path.join(d, "_fallback.json")) or {}
    _fb_map = _fb.get("map") or {}
    return jsonify({
        "key": book_key,
        "name": info.get("name") or book_key,
        "author": info.get("author", ""),
        "intro": info.get("intro", ""),
        "total": len(chapters),
        "done": done,
        "state_completed": len(completed),
        "stale_completed": stale,
        # **失败数以磁盘为准**：`_state.json` 里的 failed 可能挂着旧记录
        # （实测：2 章正文其实已下载 1 万字、能正常读，却仍被算作失败 ✗），
        # 于是检查更新会劝用户再下一次已经能读的章。这里剔掉"本机已有正文"的条目。
        "failed_count": len(_failed_live(d, failed)),
        "progress": load_json(os.path.join(d, "_progress.json")),
        "fallback": {
            "sources": _fb.get("sources") or [],
            "backfilled": len(_fb_map),
            "mapped": {c.get("url", ""): _fb_map.get(c.get("url", ""))
                       for c in chapters if c.get("url") in _fb_map},
        },
        "chapters": [
            {"index": i + 1, "name": c.get("name", ""), "url": c.get("url", ""),
             "downloaded": has_cache(c),
             "failed": c.get("url") in failed,
             "failed_reason": failed.get(c.get("url", ""), ""),
             "fb_from": (_fb_map.get(c.get("url", "")) or {}).get("uid", "")}
            for i, c in enumerate(chapters)
        ],
    })


@bp.route("/api/books/<book_key>/chapter/<int:idx>", methods=["POST"])
def api_book_chapter_retry(book_key, idx):

    book_key = _safe_seg(book_key, "书籍")
    """重新爬取单个章节（失败章节手动重爬）。同步执行，返回新内容。"""
    d = os.path.join(BOOKS_DIR, book_key)
    state = load_book_state(os.path.join(d, "_state.json"))
    if not state:
        abort(404, "书籍不存在")
    chapters = state.get("chapters", [])
    if idx < 1 or idx > len(chapters):
        abort(404, "章节序号越界")
    entry = chapters[idx - 1]
    info = state.get("book") or {}
    source_uid = info.get("source_uid") or book_key.rsplit("_", 1)[0]
    src = get_by_uid(source_uid)
    if not src:
        return _err_json(f"书源不存在: {source_uid}", 404)
    try:
        c = SourceCrawler(src)
        content = c.get_content(entry.get("url", ""), timeout=25)
        if not content.strip():
            return _err_json("内容为空，重爬失败", 500)
        # 写缓存（原子写：截断写中途崩溃会留下半截正文，被当作已下载）
        atomic_write_text(
            os.path.join(d, _cache_key(entry.get("url", "")) + ".cache"),
            f"{entry.get('name','')}\n\n{content}")
        # 更新 state：从 failed 移除，加入 completed
        completed = state.get("completed", [])
        failed = state.get("failed", {}) or {}
        failed.pop(entry.get("url", ""), None)
        if entry.get("url") not in completed:
            completed.append(entry.get("url"))
        state["completed"] = completed
        state["failed"] = failed
        state["updated_at"] = now_iso()
        atomic_write(os.path.join(d, "_state.json"), state)
        # P1-5: 写点后主动失效解析缓存（mtime 指纹亦会兜底）
        invalidate_book_state(os.path.join(d, "_state.json"))
        # 若全章完成则重建 txt
        if len(completed) >= len(chapters):
            try:
                from engine.crawler import CrawlTask
                # 必须先 _load_state() 载入目录——此前 book=None 直接 merge_txt，
                # 而 merge_txt 对空 book 直接返回 None，重建从未生效
                t = CrawlTask(src, info.get("book_url", ""), d, resume=True)
                if t._load_state():
                    t.merge_txt()
            except Exception:
                pass
        return jsonify({"ok": True, "index": idx, "name": entry.get("name", ""),
                        "content": content, "length": len(content)})
    except Exception as e:
        # 只回"重爬失败"用户没法照做（是断网？域名解析？磁盘满？）。
        # 复用既有归因：磁盘类走 app_utils，网络类走 neterr（均固定中文，脱敏）
        from engine.app_utils import disk_error_text
        from engine.neterr import classify, reason_for
        _why = disk_error_text(e)
        if not _why:
            _kind = classify(e)
            _why = reason_for(_kind) if _kind else ""
        return _err_response(e, 500, ("重爬失败：" + _why) if _why else "重爬失败")


@bp.route("/api/books/<book_key>/chapter/<int:idx>")
def api_book_chapter(book_key, idx):

    book_key = _safe_seg(book_key, "书籍")
    d = os.path.join(BOOKS_DIR, book_key)
    state = load_book_state(os.path.join(d, "_state.json"))
    if not state:
        abort(404, "书籍不存在")
    chapters = state.get("chapters", [])
    if idx < 1 or idx > len(chapters):
        abort(404, "章节序号越界")
    entry = chapters[idx - 1]
    cp = os.path.join(d, _cache_key(entry.get("url", "")) + ".cache")
    # B07: 先 stat 取文件指纹做条件校验，命中直接 304——不读正文。
    # ETag 用 (mtime_ns, size) 高精度指纹：同秒重爬/修复（int(mtime) 不变）
    # 也会产生新标识，客户端不会拿到陈旧正文。
    try:
        st = os.stat(cp)
    except OSError:
        st = None
    etag = None
    if st is not None and st.st_size > 0:
        # R78: ETag 不得含 book_key——中文目录名(爱下书_/夜天连看_…)会触发
        # HTTP 头 latin-1 编码异常(UnicodeEncodeError → 连接被断 → 章节
        # 永远读不了, 而导出 txt 不走此 header 故正常)。URL 已含 book_key,
        # (idx, mtime_ns, size) 在单 URL 内已唯一。
        etag = f'"{idx}:{st.st_mtime_ns}:{st.st_size}"'
        if request.headers.get("If-None-Match") == etag:
            return Response(status=304, headers={
                "ETag": etag, "Cache-Control": "private, max-age=3600"})
    text = ""
    if st is not None:
        try:
            with open(cp, encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError):
            text = ""  # 损坏缓存 → 视为未下载
    if not text.strip():
        # 0.55.0: 内容为空时给出**原因**（不显示空白页）。state 说已完成
        # 但磁盘没有缓存 → 说明缓存被清理/书目录被任务重建，用户可点重试
        # （POST 同路径）重新抓取，或对整本"继续下载"自愈。
        _reason = "本章尚未下载"
        if entry.get("url") in set(state.get("completed", [])):
            _reason = ("本章缓存文件已丢失（缓存被清理或书目录被任务重建），"
                       "可点「重试」重新抓取，或对整本「检查更新」自愈")
            # ── 状态跟着磁盘走（0.74.9）──────────────────────────────
            # 旧实现只把"丢失"告诉用户，`completed` 里仍留着这一条 —— 于是
            # 「检查更新 → 下载缺失」按 completed 对比，认为"一个都不缺"，
            # 用户只能逐章手点重试。这里把该章从 completed 剔除（并落盘），
            # 让缺失检测能看见它、自动补回来。**读取路径发现的事实，state 必须承认。**
            try:
                _st2 = load_book_state(os.path.join(d, "_state.json")) or {}
                _done = list(_st2.get("completed") or [])
                if entry.get("url") in _done:
                    _done.remove(entry.get("url"))
                    _st2["completed"] = _done
                    _st2["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    _sp = os.path.join(d, "_state.json")
                    atomic_write(_sp, _st2)
                    invalidate_book_state(_sp)
                    print("[chapter] %s 第%d章缓存缺失 → 已从 completed 剔除（可被"
                          "「下载缺失」自动补回）" % (book_key[:24], idx), flush=True)
            except Exception as _e:                              # noqa: BLE001
                # 自愈失败不影响读取语义：用户仍能看到原因并手动重试
                print("[chapter] 状态自愈失败：%s: %s" % (type(_e).__name__, _e),
                      flush=True)
        return jsonify({"index": idx, "name": entry.get("name", ""),
                        "downloaded": False, "content": "",
                        "reason": _reason})
    # 标题用 state 中的真实章节名（缓存文件是纯正文，首行是正文不是标题）
    title = (entry.get("name") or "").strip() or f"第{idx}章"
    content = text.strip()
    # P2-5: 已下载章节内容不可变 → 私有缓存 1h + 基于 (book_key, idx,
    # 缓存文件 mtime_ns, size) 的 ETag，支持 304。仅作用于本不可变端点；
    # 任务状态/进度等可变端点不加缓存头
    resp = jsonify({"index": idx, "name": title,
                    "downloaded": True, "content": content,
                    "prev": idx - 1 if idx > 1 else None,
                    "next": idx + 1 if idx < len(chapters) else None,
                    "total": len(chapters)})
    resp.headers["Cache-Control"] = "private, max-age=3600"
    resp.headers["ETag"] = etag
    return resp


@bp.route("/api/books/<book_key>", methods=["DELETE"])
def api_book_delete(book_key):

    book_key = _safe_seg(book_key, "书籍")
    """删除书籍数据（软删除：移入 data/trash/ 可恢复）"""
    d = os.path.join(BOOKS_DIR, book_key)
    if not os.path.isdir(d):
        abort(404, "书籍不存在")
    trash_dir = os.path.join(DATA_DIR, "trash")
    os.makedirs(trash_dir, exist_ok=True)
    dst = os.path.join(trash_dir, f"{book_key}_{now_iso().replace(':', '-')}")
    try:
        shutil.move(d, dst)
    except Exception as e:
        return _err_response(e, 500, "删除失败")
    return jsonify({"ok": True, "trash": dst})


@bp.route("/api/search-toc-count", methods=["POST"])
def api_search_toc_count():
    """获取指定书源+书籍的真实总章数（get_toc，带缓存；慢，前端异步调用）"""
    data = request.get_json(silent=True) or {}
    source_uid = data.get("source_uid")
    book_url = data.get("book_url")
    src = get_by_uid(source_uid) if source_uid else None
    if not src or not book_url:
        return _err_json("参数不完整")
    # R29(技术评审4.2): SSRF 防护
    try:
        book_url = _safe_target_url(book_url)
    except ValueError as _e:
        return _err_json(str(_e))
    with _toc_lock:
        cached = _toc_cache.get(book_url)
    if cached and time.time() - (cached.get("ts") or 0) < TOC_CACHE_TTL:
        return jsonify({"ok": True, **cached, "cached": True})
    # 单飞：同书目录抓取进行中 → 等待其完成复用结果，避免详情页并发
    # 调用 search-toc + search-toc-count 重复抓目录（代理源一次抓取 10-25s）
    _im_creator = False
    with _toc_lock:
        ev = _toc_fetching.get(book_url)
        if ev is None:
            ev = threading.Event()
            _toc_fetching[book_url] = ev
            _im_creator = True
    if not _im_creator:
        # 等待者：等创建者完成（总时限内），复用其缓存结果
        ev.wait(timeout=_TOC_FETCH_DEADLINE)
        with _toc_lock:
            # 仅当登记的事件仍是本次等待的那个才 pop——否则可能误删
            # 新一轮创建者登记的事件，导致单飞失效
            if _toc_fetching.get(book_url) is ev:
                _toc_fetching.pop(book_url, None)
            _cached2 = _toc_cache.get(book_url)
        if _cached2 and "chapters" in _cached2:
            return jsonify({"ok": True, **{k: _cached2[k] for k in
                            ("chapter_count", "last_chapter", "name", "ts")},
                            "cached": True})
        return jsonify({"error": "源站目录获取超时，请稍后重试",
                        "code": "TOC_TIMEOUT"}), 504
    ok, data = _toc_fetch_bounded(src, book_url)
    if ok:
        return jsonify({"ok": True, "chapter_count": data["chapter_count"],
                        "last_chapter": data.get("last_chapter", ""),
                        "name": data.get("name", ""), "cached": False})
    if data is None:
        return jsonify({"error": "源站目录获取超时（35 秒），"
                                 "稍后自动完成可刷新重试",
                        "code": "TOC_TIMEOUT"}), 504
    return _err_response(data, 500, "目录获取失败")


@bp.route("/api/search-toc", methods=["POST"])
def api_search_toc():
    """搜索结果的该书源完整目录（get_toc，带缓存；详情页展示章节列表）"""
    data = request.get_json(silent=True) or {}
    source_uid = data.get("source_uid")
    book_url = data.get("book_url")
    src = get_by_uid(source_uid) if source_uid else None
    if not src or not book_url:
        return _err_json("参数不完整")
    # R29(技术评审4.2): SSRF 防护
    try:
        book_url = _safe_target_url(book_url)
    except ValueError as _e:
        return _err_json(str(_e))
    with _toc_lock:
        cached = _toc_cache.get(book_url)
    # 只有带完整 chapters 列表（且非空）的条目才算目录缓存；
    # 仅含 chapter_count 的计数条目（toc-count/search-detail 写入）不算，需重新爬目录
    if cached and "chapters" in cached and cached.get("chapters") \
            and time.time() - (cached.get("ts") or 0) < TOC_CACHE_TTL:
        chs = cached.get("chapters") or []
        return jsonify({"ok": True, "name": cached.get("name", ""),
                        "chapter_count": cached.get("chapter_count", len(chs)),
                        "chapters": chs[:5000], "cached": True})
    # 单飞：同书目录正在抓取 → 等待复用（详情页 search-toc 与 toc-count 并发）
    _im_creator2 = False
    with _toc_lock:
        _ev = _toc_fetching.get(book_url)
        if _ev is None:
            _ev = threading.Event()
            _toc_fetching[book_url] = _ev
            _im_creator2 = True
    if not _im_creator2:
        _ev.wait(timeout=_TOC_FETCH_DEADLINE)
        with _toc_lock:
            _c2 = _toc_cache.get(book_url)
        if _c2 and "chapters" in _c2:
            chs2 = _c2.get("chapters") or []
            return jsonify({"ok": True, "name": _c2.get("name", ""),
                            "chapter_count": _c2.get("chapter_count", len(chs2)),
                            "chapters": chs2[:5000], "cached": True})
        return jsonify({"error": "源站目录获取超时，请稍后重试",
                        "code": "TOC_TIMEOUT"}), 504
    ok, data = _toc_fetch_bounded(src, book_url)
    if ok:
        chs3 = data.get("chapters") or []
        return jsonify({"ok": True, "name": data.get("name", ""),
                        "chapter_count": data.get("chapter_count", len(chs3)),
                        "chapters": chs3[:5000], "cached": False})
    if data is None:
        return jsonify({"error": "源站目录获取超时（35 秒），"
                                 "稍后自动完成可刷新重试",
                        "code": "TOC_TIMEOUT"}), 504
    return _err_response(data, 500, "目录获取失败")


@bp.route("/api/search-detail", methods=["POST"])
def api_search_detail():
    """单书详情（搜索结果懒加载：最新章节/更新时间/字数/章节数）"""
    data = request.get_json(silent=True) or {}
    source_uid = data.get("source_uid")
    book_url = data.get("book_url")
    src = get_by_uid(source_uid) if source_uid else None
    if not src or not book_url:
        return _err_json("参数不完整")
    # SSRF 防护：与 /api/search-toc、/api/search-toc-count 对齐（此前遗漏）
    try:
        book_url = _safe_target_url(book_url, "书籍URL")
    except ValueError:
        return _err_json("非法 URL")
    try:
        c = SourceCrawler(src)
        book = c.get_book(book_url, fast=True)
        m = re.search(r'(?:共|全书|总)?(\d+)\s*(?:章|节)', book.last_chapter + ' ' + book.kind)
        cc = int(m.group(1)) if m else 0
        if cc:
            with _toc_lock:
                # 不覆盖已有的完整目录缓存（计数条目缺 chapters，会污染 search-toc/read 的命中）
                prev = _toc_cache.get(book_url)
                if not (prev and "chapters" in prev):
                    _toc_cache[book_url] = {"chapter_count": cc,
                                            "last_chapter": book.last_chapter,
                                            "name": book.name,
                                            "ts": time.time()}
            _toc_cache_save()
        return jsonify({
            "ok": True,
            "name": book.name,
            "cover": getattr(book, "cover", "") or "",
            "last_chapter": book.last_chapter,
            "update_time": book.update_time,
            "word_count": book.word_count,
            "chapter_count": cc,
            "intro": book.intro,
        })
    except Exception as e:
        return _err_response(e, 500, "详情获取失败")


@bp.route("/api/books/<book_key>/reclean", methods=["POST"])
def api_book_reclean(book_key):
    """**净化已下载正文**（去广告/分隔线/站点残留），并重建全文导出。

    为什么需要它：净化管线是逐年累加改进的，**老缓存里带着当时清不掉的垃圾**
    （实测用户导出的《在美漫当心灵导师的日子》里有 5014 行纯 `─` 分隔线与成片的
    "换源 app"推广句）。只改净化规则救不了这些文件——必须能把**已经落盘的内容**
    重新过一遍管线。

    用法：
      · `?dry_run=1`（默认）：只统计会给什么结果，**不改任何文件**；
      · 真正执行：`{"dry_run": false}` 或 `?dry_run=0`，逐章改写缓存并重建 book.txt。

    安全：只对**已下载**的章节缓存做幂等净化（同一份文本跑两遍结果一致），
    不动书架/进度/失败记录；改写失败按章记录原因，不谎报成功。
    """
    book_key = _safe_seg(book_key, "书籍")
    data = request.get_json(silent=True) or {}
    dry = data.get("dry_run")
    if dry is None:
        dry = str(request.args.get("dry_run", "1")).lower() not in ("0", "false", "no")
    dry = bool(dry)

    d = os.path.join(BOOKS_DIR, book_key)
    state = load_book_state(os.path.join(d, "_state.json"))
    if not state:
        abort(404, "书籍不存在")

    from engine.cleaner import clean_text
    from engine.app_utils import cache_key_of, atomic_write_text

    chapters = state.get("chapters", []) or []
    _title_re = re.compile(r'^(第[一二三四五六七八九十百千万零两\d]+[章卷篇节]|序章|楔子|番外|尾声|终章|后记|外传)')
    changed, skipped, failed = 0, 0, []
    removed_chars, removed_lines = 0, 0
    samples = []

    for idx, c in enumerate(chapters):
        if dry and len(samples) >= 3 and changed >= 400:
            break          # 干跑不必扫全库：够给用户看规模即可
        cp = os.path.join(d, cache_key_of(c.get("url", "")) + ".cache")
        if not os.path.isfile(cp):
            continue
        try:
            with open(cp, encoding="utf-8") as f:
                raw = f.read()
        except (OSError, UnicodeDecodeError) as e:
            failed.append({"index": idx + 1, "reason": f"{type(e).__name__}: {e}"})
            continue
        # 缓存首行可能是章节名（重爬单章写入的格式）——**标题不参与净化**，
        # 否则 L3 的"正文内标题残留"规则会把标题本身删掉
        head, body = "", raw
        if raw:
            first, _sep, rest = raw.partition("\n")
            if _title_re.match(first.strip()) and len(first.strip()) <= 40:
                head, body = first + "\n", rest
        cleaned = clean_text(body)
        if cleaned.strip() == body.strip():
            skipped += 1
            continue
        # 统计"删掉了什么"。判据要精确，否则会把"改过的行"报成"删掉的行"：
        #   · 整行垃圾：这一行单独过净化后**什么都不剩**（纯广告/纯分隔线）
        #   · 行内广告：行还在，但里面的广告被摘掉（只并入 removed_chars）
        for line in body.split("\n"):
            if not line.strip():
                continue
            if clean_text(line).strip() == "":
                removed_lines += 1
                if len(samples) < 3:
                    samples.append(line.strip()[:80])
        removed_chars += max(0, len(body) - len(cleaned))
        changed += 1
        if not dry:
            try:
                atomic_write_text(cp, head + cleaned)
            except Exception as e:      # noqa: BLE001
                failed.append({"index": idx + 1, "reason": f"{type(e).__name__}: {e}"})
                changed -= 1

    rebuilt = False
    if not dry and changed:
        # 全文导出跟着更新（导出读的是缓存，缓存变了 txt 就是旧的）
        try:
            src = get_by_uid(state.get("book", {}).get("source_uid")
                            or book_key.rsplit("_", 1)[0])
            if src:
                from engine.crawler import CrawlTask
                t = CrawlTask(src, state.get("book", {}).get("book_url", ""), d, resume=True)
                if t._load_state():
                    t.merge_txt()
                    rebuilt = True
        except Exception as e:          # noqa: BLE001
            failed.append({"index": 0, "reason": f"重建全文失败：{type(e).__name__}: {e}"})

    return jsonify({
        "ok": True, "dry_run": dry, "chapters": len(chapters),
        "changed": changed, "already_clean": skipped,
        "removed_lines": removed_lines, "removed_chars": removed_chars,
        "samples": samples, "failed": failed, "txt_rebuilt": rebuilt,
    })


@bp.route("/api/books/<book_key>/check-update", methods=["POST"])
def api_book_check_update(book_key):

    book_key = _safe_seg(book_key, "书籍")
    """检查更新（异步）：获取最新目录，若有新章节或失败章节则创建增量爬取任务。
    立即返回 202，前端轮询 /check-status 获取结果。"""
    d = os.path.join(BOOKS_DIR, book_key)
    state = load_book_state(os.path.join(d, "_state.json"))
    if not state:
        abort(404, "书籍不存在")
    info = state.get("book") or {}
    book_url = info.get("book_url")
    if not book_url:
        abort(404, "书籍数据不完整")
    # 判定「已有检查」与登记 job 必须在同一临界区：拆成两个 with _lock 时，
    # 两个并发请求都能越过 status != "checking" 判定、各自登记并各起一个
    # worker（双跑、结果互相覆盖）。
    job = {"status": "checking", "new_chapters": 0, "retry_failed": 0,
           "task_id": None, "message": "检查中…", "error": "",
           "ts": now_iso()}
    with _lock:
        exist = _check_jobs.get(book_key)
        if exist and exist.get("status") == "checking":
            return jsonify({"ok": True, "checking": True,
                            "message": "已有检查在进行中"}), 202
        _check_jobs[book_key] = job
    th = threading.Thread(target=_run_check_update, args=(book_key,),
                          daemon=True, name=f"check-{book_key[:16]}")
    try:
        th.start()
    except Exception as e:
        # 启动失败：job 已在同锁内登记为 checking，若不改状态会永久停在
        # checking，后续请求恒返回 202「已有检查在进行中」且无法重试。
        # 置为 error（与 _run_check_update 的失败态一致）后即可重试。
        with _lock:
            _check_jobs[book_key] = {
                "status": "error", "new_chapters": 0, "retry_failed": 0,
                "task_id": None, "message": "检查任务启动失败",
                "error": "检查任务启动失败", "ts": now_iso()}
        return _err_response(e, 500, "检查任务启动失败")
    return jsonify({"ok": True, "checking": True}), 202


@bp.route("/api/books/<book_key>/check-status")
def api_book_check_status(book_key):

    book_key = _safe_seg(book_key, "书籍")
    """轮询检查更新状态"""
    with _lock:
        job = _check_jobs.get(book_key)
    if not job:
        return jsonify({"ok": False, "status": "none",
                        "message": "没有进行中的检查"}), 404
    return jsonify({"ok": True, **job})


@bp.route("/api/books/<book_key>/download-missing", methods=["POST"])
def api_book_download_missing(book_key):

    book_key = _safe_seg(book_key, "书籍")
    """确认补充下载：用检查更新检测出的缺失章节创建增量任务"""
    with _lock:
        miss = _check_missing.get(book_key)
        if not miss:
            return _err_json("没有待下载的缺失章节（请先检查更新）")
        chapters = list(miss.get("chapters") or [])
    if not chapters:
        return _err_json("没有缺失章节")
    book_url = miss.get("book_url", "")
    source_uid = miss.get("source_uid", "")
    # check_toc 随任务创建一并登记（持锁且在 worker 启动前），消除
    # "worker 先 pop 到 None → 增量退化为整书全量重爬" 的 TOCTOU
    tid, err, running_id = _create_task(source_uid, book_url,
                                        check_toc=chapters)
    if err:
        return _err_json(err, 500)
    with _lock:
        _check_missing.pop(book_key, None)
    return jsonify({"ok": True, "task_id": tid, "chapters": len(chapters)})


# 固定条带锁：book_key 经稳定 md5 映射到固定 64 把锁之一，同一 book_key
# 永久映射同一对象；表不增长、不淘汰 → 从根上消除"持有者/等待者的锁被
# 驱逐后拿到不同锁对象 → 单飞退化"的竞态，无需 `not locked()` 近似租约。
_EXPORT_LOCK_STRIPES = 64
_export_lock_stripes = tuple(threading.Lock() for _ in range(_EXPORT_LOCK_STRIPES))


def _export_lock(book_key):
    """稳定条带锁：同一 book_key 永久映射到固定锁对象（表不增长/不淘汰）"""
    idx = int(hashlib.md5(str(book_key).encode("utf-8", "surrogatepass"))
              .hexdigest(), 16) % _EXPORT_LOCK_STRIPES
    return _export_lock_stripes[idx]


def _rebuild_book_txt(book_key, d, state):
    """按需重建 book.txt（导出版本不一致时）。返回 (ok, err)。

    - 书源存在：复用 resume 任务触发 merge_txt（与单章重爬后的重建同款），
      必须先 _load_state() 载入目录，否则 book=None 直接返回 None；
    - 书源不存在（旧书/源已被删除）：内容已在本地 .cache，无需书源即可
      离线合并，不再对无源旧书直接报 500；
    - 显式返回失败原因（此前 except 静默 pass，导出侧只能靠文件是否存在
      猜测，无法区分"源不存在/合并失败/无可合并内容"）
    """
    chapters = state.get("chapters") or []
    if not chapters:
        return False, "书籍状态不完整，无法重建全文"
    src = get_by_uid((state.get("book") or {}).get("source_uid")
                     or book_key.rsplit("_", 1)[0])
    if not src:
        return _rebuild_book_txt_local(book_key, d, chapters)
    try:
        from engine.crawler import CrawlTask
        t = CrawlTask(src, (state.get("book") or {}).get("book_url", ""),
                      d, resume=True)
        if not t._load_state():
            # 目录已从磁盘读出（chapters 非空）→ 退回本地离线合并
            return _rebuild_book_txt_local(book_key, d, chapters)
        out = t.merge_txt()
        if not out:
            return False, "无可合并的章节缓存"
        return True, ""
    except Exception as e:
        print(f"[export] {book_key} 全文重建失败: {type(e).__name__}: {e}",
              flush=True)
        return False, "全文重建失败，请稍后重试"


def _rebuild_book_txt_local(book_key, d, chapters):
    """旧书无源（或任务态不可用）时的本地合并：仅依赖目录 + .cache，不触网。"""
    try:
        out = _merge_book_txt(d, chapters)
    except Exception as e:
        print(f"[export] {book_key} 本地全文合并失败: {type(e).__name__}: {e}",
              flush=True)
        return False, "全文重建失败，请稍后重试"
    if not out:
        return False, "无可合并的章节缓存"
    return True, ""


@bp.route("/api/books/<book_key>/txt")
def api_book_txt(book_key):

    book_key = _safe_seg(book_key, "书籍")
    d = os.path.join(BOOKS_DIR, book_key)
    p = os.path.join(d, "book.txt")
    state = load_book_state(os.path.join(d, "_state.json")) or {}
    chapters = state.get("chapters") or []
    # 导出版本机制（2026-09-10）：新鲜度判据改为"由章节缓存派生的内容版本"
    # （engine.crawler.export_is_fresh），取代此前的 updated_at/mtime 时间戳
    # 推断——重爬单章、补章、目录重排都会改变版本，时钟回拨不再误判。
    stale_err = ""
    if not _export_is_fresh(d, chapters, p):
        if chapters:
            with _export_lock(book_key):
                # 双检：等锁期间可能已被其它请求重建完成（单飞收口）
                if not _export_is_fresh(d, chapters, p):
                    ok, err = _rebuild_book_txt(book_key, d, state)
                    if not ok:
                        stale_err = err
    if not os.path.exists(p):
        # 显式失败处理：区分"无可导出内容"与"重建失败"
        if stale_err:
            return _err_json(stale_err, 500)
        abort(404, "全文尚未生成")
    name = (state.get("book") or {}).get("name") or book_key
    resp = send_file(p, as_attachment=True, download_name=f"{name}.txt",
                     mimetype="text/plain; charset=utf-8")
    if stale_err or not _export_is_fresh(d, chapters, p):
        # 重建失败但有旧全文：显式标注陈旧（客户端/日志可辨），仍给可用内容
        resp.headers["X-Export-Stale"] = "1"
        if stale_err:
            resp.headers["X-Export-Error"] = "1"
    return resp


@bp.route("/api/books/<book_key>/progress", methods=["GET"])
def api_book_progress_get(book_key):
    book_key = _safe_seg(book_key, "书籍")
    data = _read_json(BOOK_PROGRESS_FILE, {})
    return jsonify({"ok": True, **data.get(book_key, {})})


@bp.route("/api/books/<book_key>/progress", methods=["POST"])
def api_book_progress_save(book_key):
    book_key = _safe_seg(book_key, "书籍")
    body = request.get_json(silent=True) or {}
    idx = int(body.get("idx") or 0)
    pct = int(body.get("pct") or 0)
    name = str(body.get("name") or "")[:100]
    # 兜底：章节序号必须 >= 1。客户端在"读进度失败"时可能回退到 0/第 1 章，
    # 那种写入会把两端已有进度一起清掉（用户实测的"重连后进度被重置"）。
    # 章节 1 是合法值（真的在第一 章），但 0/负数一定是无意义写入。
    if idx < 1:
        return jsonify({"ok": False, "error": "无效的章节序号"}), 400
    pct = max(0, min(100, pct))

    def _merge(data):
        data = data or {}
        data[book_key] = {"idx": idx, "pct": pct, "name": name, "ts": time.time()}
        # 限 500 条
        if len(data) > 500:
            for k in sorted(data, key=lambda x: data[x].get("ts", 0))[:100]:
                data.pop(k, None)
        return data

    # 读-改-写在同一临界区（A05）：否则并发保存不同书籍时后写覆盖先写；
    # 写盘失败必须显式失败，不得返回 ok:true 让前端以为已同步
    try:
        update_json(BOOK_PROGRESS_FILE, _merge, {})
    except Exception as e:
        # A05 脱敏（P2-8，同 _err_response 模式）：OSError 等原始异常消息含
        # 服务器绝对路径，只记服务端日志；客户端拿通用文案。
        # 响应形状保持 {"ok": False, "error": ...}（前端按 d.ok !== true 判失败）
        import traceback
        traceback.print_exc()
        print(f"[error] 进度保存失败: {type(e).__name__}: {e}", flush=True)
        return jsonify({"ok": False, "error": "进度保存失败，请稍后重试"}), 500
    return jsonify({"ok": True})


