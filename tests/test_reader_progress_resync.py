# -*- coding: utf-8 -*-
"""阅读进度"重连即被重置"根因回归（离线）

用户实测（**手机端 = 浏览器**，不是 App）：每次手机端重连都会导致阅读进度被
重置，且两端进度不一致。

根因（本轮定位）：进度**读取失败**时前端直接回退到"本机/第 1 章"，随后用户
一滚动就把回退位置 POST 回服务器——于是两端共用的进度一起被改成第 1 章。

    // templates/reader.html（旧）
    fetch('/api/books/<key>/progress')
      .then(...).then(d => { ...应用...; _gotoLocal(); })
      .catch(() => _gotoLocal());      // ← 重连瞬间 GET 失败 = 回退 = 之后覆盖进度

    // templates/manga_reader.html（旧）
    async function restoreProgress() {
      try { ... } catch (e) {}          // ← 失败后 chIdx0 仍为 0 → 打开第 1 话
    }
    function saveProgress() { ... _postHistory(payload, payload, false); }  // 无条件写回

修复契约（本文件锁定）：
  1. 读到服务器进度之前**禁止写服务器**（读取失败只存本机）；
  2. 读取失败要退避重试，并在 online / 回到前台时补读；
  3. 迟到读到时：用户仍在回退章 → 跟随服务器进度；已自行导航 → 尊重用户位置，
     但同样恢复"可写"状态（否则之后永不回传）；
  4. 服务端兜底：小说进度禁止写入 idx < 1（"没有进度"不得覆盖已有进度）。
"""
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READER = open(os.path.join(ROOT, "templates", "reader.html"),
              encoding="utf-8").read()
MANGA_READER = open(os.path.join(ROOT, "templates", "manga_reader.html"),
                    encoding="utf-8").read()


# ── 1. 小说阅读器：读取失败不得写回 ──────────────────────────
def test_novel_savepos_gated_on_progress_ready():
    m = re.search(r"function savePos\(\) \{.*?\n\}", READER, re.S)
    assert m, "未找到 savePos"
    body = m.group(0)
    assert "_progressReady" in body, "savePos 未按 _progressReady 把关"
    assert re.search(r"if \(_progressReady && navigator\.onLine !== false\)", body), \
        "savePos 必须在读到服务器进度之后才写服务器"


def test_novel_restore_never_marks_ready_on_failure():
    m = re.search(r"const _readProgress = \(attempt\) => \{.*?\n    \};",
                  READER, re.S)
    assert m, "未找到 _readProgress 恢复逻辑"
    body = m.group(0)
    # 成功路径设置可写；失败路径不得设置
    assert "_progressReady = true;" in body
    catch = body[body.index(".catch("):]
    assert "_progressReady = true" not in catch, \
        "读取失败的 catch 分支绝不能把状态置为可写（会覆盖两端进度）"
    # 失败要重试
    assert "attempt < 6" in body and "setTimeout" in body, "读取失败未退避重试"
    # 失败时记录回退章，供迟到同步判断用户是否已导航
    assert "_progressFallbackIdx" in catch, "失败分支未记录回退章"


def test_novel_retries_on_online_and_foreground():
    assert re.search(r"addEventListener\('online'", READER), "缺少 online 补读"
    # 回到前台补读：visibilitychange 监听里必须有 _retryProgressRead
    # （文件里现在有两个 visibilitychange 监听：切后台 → beacon 保存；
    #   回前台 → 补读进度，两者都要在）
    assert re.search(r"visibilitychange[\s\S]{0,400}?_retryProgressRead", READER), \
        "回到前台时应补读进度（否则重连后一直不可写）"
    assert "window._retryProgressRead" in READER


def test_novel_explicit_chapter_allows_write():
    """显式点章进入是明确意图：应立即可写（否则点了章却不记进度）"""
    m = re.search(r"if \(_chParamPending >= 1 && _chParamPending <= BOOK\.total\) \{.*?\n  \}",
                  READER, re.S)
    assert m and "_progressReady = true;" in m.group(0), \
        "显式点章进入应允许写回进度"


# ── 2. 漫画阅读器：同类门控 ──────────────────────────────────
def test_manga_save_progress_gated():
    m = re.search(r"function saveProgress\(\) \{.*?\n\}", MANGA_READER, re.S)
    assert m and "if (!_histReady) return;" in m.group(0), \
        "saveProgress 必须在读到历史后才写服务器"


def test_manga_restore_sets_ready_only_on_success():
    m = re.search(r"async function restoreProgress\(attempt = 0\) \{.*?\n\}",
                  MANGA_READER, re.S)
    assert m, "未找到 restoreProgress"
    body = m.group(0)
    assert "_histReady = true;" in body
    catch = body[body.index("} catch (e) {"):]
    assert "_histReady = true" not in catch, "失败分支不得置为可写"
    assert "attempt < 6" in catch and "setTimeout" in catch, "失败未退避重试"
    # 迟到成功且用户未导航时的跳转分支也必须置可写（跳转后 return）
    assert re.search(r"_histReady = true;\s*//[^\n]*\n\s*jumpChapter", body), \
        "跳转分支必须在 return 前置可写，否则之后永不回传"


def test_manga_explicit_chapter_restores_page():
    """显式点章进入时，同话要恢复章内页码（否则每次都从 P1 开始并写回 P1）"""
    assert re.search(r"else if \(rec\.idx === chIdx0 && _pg > 1\) \{\s*\n\s*pg0 = _pg;",
                     MANGA_READER), "显式章节未恢复同话页码"
    assert re.search(r"addEventListener\('online'", MANGA_READER), "缺少 online 补读"


# ── 3. 服务端兜底：idx < 1 不得覆盖已有进度 ──────────────────
@pytest.fixture()
def client(tmp_path, monkeypatch):
    import server.novel_api as na
    prog = tmp_path / "book_progress.json"
    prog.write_text(json.dumps({"k1": {"idx": 7, "pct": 42, "name": "第七章",
                                       "ts": 1700000000.0}}, ensure_ascii=False),
                    encoding="utf-8")
    monkeypatch.setattr(na, "BOOK_PROGRESS_FILE", str(prog), raising=False)
    import app
    app.app.config["TESTING"] = True
    return {"c": app.app.test_client(), "p": prog}


def test_server_rejects_idx_zero_write(client):
    r = client["c"].post("/api/books/k1/progress",
                         json={"idx": 0, "pct": 0, "name": ""})
    assert r.status_code == 400, r.get_data(as_text=True)[:120]
    assert (r.get_json() or {}).get("ok") is not True
    d = json.loads(client["p"].read_text(encoding="utf-8"))
    assert d["k1"]["idx"] == 7, "已有的第 7 章进度不得被 idx=0 的写入清掉"


def test_server_accepts_valid_write_and_clamps_pct(client):
    r = client["c"].post("/api/books/k1/progress",
                         json={"idx": 9, "pct": 250, "name": "第九章"})
    assert r.status_code == 200 and r.get_json().get("ok") is True
    d = json.loads(client["p"].read_text(encoding="utf-8"))
    assert d["k1"]["idx"] == 9
    assert d["k1"]["pct"] == 100, "章内百分比应被夹到 0..100"
    assert d["k1"]["name"] == "第九章"


def test_server_progress_get_returns_record(client):
    j = client["c"].get("/api/books/k1/progress").get_json()
    assert j["ok"] is True and j["idx"] == 7 and j["pct"] == 42


# ── 4. 退出即更新阅读记录（jm 实测："读到一半退出后不更新"）──────────
def test_manga_explicit_branch_restores_history():
    """显式章节（书库/详情带 ?ch=N）也必须调用 restoreProgress。

    否则 _histReady 永远为 false，"读到进度前不写服务器"的门会把之后所有
    阅读记录写入全部挡掉——实测缺陷：jm 从书库进入后读到一半退出不更新记录。
    """
    m = re.search(r"async function loadDetail\(\) \{.*?\n\}", MANGA_READER, re.S)
    assert m, "未找到 loadDetail"
    body = m.group(0)
    # 两个分支都要 restoreProgress
    assert body.count("restoreProgress()") >= 2, \
        "显式章节分支未读历史（会连带挡掉所有历史写入）"
    assert re.search(r"if \(!_urlChExplicit\) \{.*?restoreProgress\(\)", body, re.S), \
        "非显式分支缺少 restoreProgress"
    # 0.64.0：gotoChapter 增加"是否显式打开"参数（显式才写回 P1，自动续读不覆盖记录）
    m2 = re.search(r"\} else \{.*?gotoChapter\(_fallbackChIdx(?:,\s*(?:true|false))?\);",
                   body, re.S)
    assert m2 and "restoreProgress()" in m2.group(0), "显式分支缺少 restoreProgress"


def test_manga_unload_uses_beacon():
    """退出/切后台必须用 sendBeacon（keepalive），普通 fetch 卸载时会被取消"""
    assert "function saveProgressBeacon()" in MANGA_READER
    assert "navigator.sendBeacon" in MANGA_READER
    assert re.search(r"addEventListener\('pagehide', saveProgressBeacon\)", MANGA_READER)
    assert re.search(r"addEventListener\('beforeunload', saveProgressBeacon\)", MANGA_READER)
    assert re.search(r"if \(document\.hidden\) saveProgressBeacon\(\)", MANGA_READER), \
        "切后台应走 beacon 保存"
    assert "keepalive: true" in MANGA_READER, "beacon 不可用时需 keepalive 兜底"
    # 心跳：让阅读记录接近实时。0.64.0 起心跳走 scheduleProgressSave（领先写 + 2s 节流，
    # 位置没变不写），比每 15s 无条件写一次更及时也更省。
    assert re.search(
        r"setInterval\(\(\) => \{[^}]*?(scheduleProgressSave|saveProgress)\(\);",
        MANGA_READER, re.S), "缺少阅读心跳保存"
    assert "function scheduleProgressSave()" in MANGA_READER, \
        "缺少实时保存调度（位置一变就先写）"
    assert "_SAVE_MIN_GAP" in MANGA_READER, "缺少两次自动保存之间的最小间隔"


def test_novel_unload_uses_beacon_and_heartbeat():
    assert "function savePosBeacon()" in READER
    assert "navigator.sendBeacon" in READER and "keepalive: true" in READER
    assert re.search(r"addEventListener\('pagehide', savePosBeacon\)", READER)
    assert re.search(r"addEventListener\('beforeunload', savePosBeacon\)", READER)
    assert re.search(r"setInterval\(\(\) => \{[^}]*savePos\(\);", READER, re.S), \
        "缺少阅读心跳保存"


def test_beacon_path_keeps_progress_gate():
    """beacon 同样受"读到进度前不写"的门约束（不能绕过防覆盖保护）"""
    m = re.search(r"function saveProgressBeacon\(\) \{.*?\n\}", MANGA_READER, re.S)
    assert m and "if (!_histReady) return;" in m.group(0)
    m2 = re.search(r"function savePosBeacon\(\) \{.*?\n\}", READER, re.S)
    assert m2 and "_progressReady" in m2.group(0)
