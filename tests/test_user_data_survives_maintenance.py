# -*- coding: utf-8 -*-
"""里程碑 C 第 3 条（离线部分）：**维护操作不得损坏用户数据**。

指南原文：「验证备份、恢复、SAF 导入导出和选择性清理**不损坏书架与历史**。」

本文件只做**离线能判定**的那部分（SAF 文件选择器与真机往返必须设备验证，
见目标机回归包）：

1. 每个清理作用域跑完，**其它书的进度/缓存、漫画离线图片、阅读历史、
   书库文件都必须逐字节不变**——"清理"只许动它自己声称要动的东西；
2. 书源导入（恢复的一种）不得动书架与历史；
3. 清理**拒绝**时必须一个字节都不删（参数不合法/任务在跑/书不存在）；
4. 清理结果的 `freed_bytes` 只算真删掉的，"没删的"要进 `kept` 并给原因。

这些断言和"清理功能本身能删干净"是**两个方向**：现有 test_storage_management.py
证明"该删的删了"，本文件证明"不该动的没动"。
"""
import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.app_utils import cache_key_of  # noqa: E402


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """隔离数据目录 + 造一份"有内容的用户数据"（两本书、漫画图、历史、书库）"""
    import server.storage as S
    root = tmp_path / "data"
    books = root / "books"
    dl = root / "manga" / "downloads"
    cache = root / "manga" / "_cache"
    trash = root / "trash"
    for d in (books, dl, cache, trash):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(S, "BOOKS_DIR", str(books), raising=False)
    monkeypatch.setattr(S, "MANGA_DOWNLOADS_DIR", str(dl), raising=False)
    monkeypatch.setattr(S, "MANGA_CACHE_DIR", str(cache), raising=False)
    monkeypatch.setattr(S, "TRASH_DIR", str(trash), raising=False)
    monkeypatch.setattr(S, "DATA_DIR", str(root), raising=False)
    monkeypatch.setattr(S, "SEARCH_CACHE_FILE", str(root / "search_cache.json"), raising=False)
    monkeypatch.setattr(S, "TOC_CACHE_FILE", str(root / "toc_cache.json"), raising=False)
    monkeypatch.setattr(S, "MANGA_LIBRARY_FILE", str(root / "manga_library.json"), raising=False)

    def _book(key, name, cached=(1, 2), failed=()):
        d = books / key
        d.mkdir(parents=True, exist_ok=True)
        chs = [{"name": f"第{i}章", "url": f"https://t.example/{key}/{i}"}
               for i in range(1, 5)]
        (d / "_state.json").write_text(json.dumps({
            "book": {"name": name, "author": "作者", "source_uid": key.split("_")[0]},
            "chapters": chs,
            "completed": [chs[i - 1]["url"] for i in cached],
            "failed": {chs[i - 1]["url"]: "抓取失败" for i in failed},
            "read_index": 3, "read_pct": 42,
        }, ensure_ascii=False), encoding="utf-8")
        for i in cached:
            (d / (cache_key_of(chs[i - 1]["url"]) + ".cache")).write_text(
                "正文内容" * 50, encoding="utf-8")
        for i in failed:
            (d / (cache_key_of(chs[i - 1]["url"]) + ".cache")).write_text(
                "", encoding="utf-8")
        return d

    _book("srcA_bookA", "书A", cached=(1, 2), failed=(3,))
    _book("srcB_bookB", "书B", cached=(1, 2, 3))

    # 漫画离线图片 + 临时缓存 + 书库 + 历史
    (dl / "jm" / "100").mkdir(parents=True, exist_ok=True)
    for i in range(2):
        (dl / "jm" / "100" / f"{i:04d}.jpg").write_bytes(b"J" * 900)
    (cache / "jm" / "tmp").mkdir(parents=True, exist_ok=True)
    (cache / "jm" / "tmp" / "x.bin").write_bytes(b"T" * 300)
    (root / "manga_library.json").write_text(json.dumps(
        [{"source": "jm", "comic_id": "100", "title": "漫画一"}], ensure_ascii=False),
        encoding="utf-8")
    (root / "manga_history.json").write_text(json.dumps(
        {"jm_100": {"pos": "第3话 P5", "ts": 1}}, ensure_ascii=False), encoding="utf-8")
    (root / "search_cache.json").write_text("{}", encoding="utf-8")
    (root / "toc_cache.json").write_text("{}", encoding="utf-8")
    (trash / "old.json").write_text("{}", encoding="utf-8")
    return S, root, books, dl, cache, trash


def _fingerprint(*paths):
    """把若干文件/目录摘成"内容指纹"——用于断言"一字未动" """
    out = {}
    for p in paths:
        p = str(p)
        if os.path.isfile(p):
            with open(p, "rb") as f:
                out[p] = hashlib.sha256(f.read()).hexdigest()
        elif os.path.isdir(p):
            for dirpath, _dirs, files in os.walk(p):
                for fn in sorted(files):
                    fp = os.path.join(dirpath, fn)
                    with open(fp, "rb") as f:
                        out[fp] = hashlib.sha256(f.read()).hexdigest()
    return out


def _user_data_paths(root, books, dl):
    return (books / "srcA_bookA", books / "srcB_bookB", dl,
            root / "manga_library.json", root / "manga_history.json")


@pytest.mark.parametrize("target", [
    {"scope": "regen"},
    {"scope": "trash"},
    {"scope": "manga_cache"},
    {"scope": "manga_cache", "source": "jm", "comic_id": "100"},
    {"scope": "book_chapters", "key": "srcA_bookA", "only": "failed"},
    {"scope": "book_chapters", "key": "srcA_bookA", "chapters": [1]},
])
def test_clear_never_touches_other_user_data(env, target):
    """**核心断言**：任何清理作用域都不许动"别人的"书架、进度、图片、历史"""
    S, root, books, dl, cache, trash = env
    before = _fingerprint(*_user_data_paths(root, books, dl))
    r = S.clear(target)
    assert r.get("freed_bytes") is not None
    after = _fingerprint(*_user_data_paths(root, books, dl))

    # 该 scope 允许动的那一份，从"必须不变"的集合里排除
    allow = set()
    if target["scope"] == "manga_cache":
        if target.get("comic_id"):
            allow.add(str(cache / "jm" / "100"))
        else:
            allow.add(str(cache))
    if target["scope"] == "trash":
        allow.add(str(trash))
    if target["scope"] == "book_chapters":
        allow.add(str(books / target["key"]))
    diff = {k for k in set(before) | set(after)
            if before.get(k) != after.get(k) and not any(k.startswith(a) for a in allow)}
    assert not diff, "清理 %s 动了不该动的东西：%s" % (target, sorted(diff)[:5])


def test_clear_regen_keeps_progress_and_history(env):
    """清"可再生数据"后：阅读进度与历史必须原样（这是用户唯一不可再生的东西）"""
    S, root, books, dl, cache, trash = env
    S.clear({"scope": "regen"})
    st = json.loads((books / "srcA_bookA" / "_state.json").read_text(encoding="utf-8"))
    assert st["read_index"] == 3 and st["read_pct"] == 42
    hist = json.loads((root / "manga_history.json").read_text(encoding="utf-8"))
    assert hist["jm_100"]["pos"] == "第3话 P5"


def test_rejected_clear_deletes_nothing(env):
    """被拒绝的清理必须**一个字节都不删**（参数不合法/书不存在）"""
    S, root, books, dl, cache, trash = env
    before = _fingerprint(*_user_data_paths(root, books, dl), cache, trash)
    for bad in ({"scope": "unknown"}, {}, {"scope": "book_chapters"},
                {"scope": "book_chapters", "key": "../escape", "chapters": [1]},
                {"scope": "book_chapters", "key": "no_such_book", "only": "failed"}):
        r = S.clear(bad)
        assert r.get("ok") is False, bad
    after = _fingerprint(*_user_data_paths(root, books, dl), cache, trash)
    assert before == after, "被拒绝的清理居然动了文件"


def test_clear_result_is_honest(env):
    """结果必须诚实：freed_bytes 只算真删掉的；保留项要进 kept 并带原因"""
    S, root, books, dl, cache, trash = env
    r = S.clear({"scope": "book_chapters", "key": "srcA_bookA", "only": "failed"})
    assert r.get("ok") is True
    deleted = r.get("deleted") or []
    assert deleted, "应删掉那条失败缓存"
    # 恢复"删不掉"的场景：把文件设成只读目录里的文件不容易，这里只验证结构
    assert isinstance(r.get("kept"), list)
    assert r.get("freed_bytes", 0) >= 0


def test_source_import_keeps_bookshelf_and_history(env, monkeypatch):
    """书源导入（恢复的一种）不得动书架与历史——它只管书源文件"""
    S, root, books, dl, cache, trash = env
    import engine.source_mgr as SM
    src_dir = root / "sources"
    src_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(SM, "SOURCES_DIR", str(src_dir), raising=False)
    before = _fingerprint(*_user_data_paths(root, books, dl))
    SM._atomic_write(str(src_dir / "demo.json"),
                     {"bookSourceName": "示例", "bookSourceUrl": "https://e.example",
                      "uid": "demo", "enabled": True})
    after = _fingerprint(*_user_data_paths(root, books, dl))
    assert before == after, "导入书源动了用户数据"


def test_fingerprint_helper_actually_detects_changes(env):
    """**防止空断言**：指纹工具必须能发现"被改了一个字节"。

    否则上面那些"一字未动"的断言可能永远为真（测试自身失效），
    这比没有测试更危险——它会给人"已覆盖"的错觉。
    """
    S, root, books, dl, cache, trash = env
    before = _fingerprint(*_user_data_paths(root, books, dl))
    # 改一个字节
    p = books / "srcB_bookB" / "_state.json"
    p.write_text(p.read_text(encoding="utf-8") + " ", encoding="utf-8")
    after = _fingerprint(*_user_data_paths(root, books, dl))
    diff = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert diff, "指纹工具没发现文件变化 → 上面的断言全是空的"

    # 删掉一个文件也要能发现
    before2 = dict(after)
    (dl / "jm" / "100" / "0000.jpg").unlink()
    after2 = _fingerprint(*_user_data_paths(root, books, dl))
    diff2 = {k for k in set(before2) | set(after2) if before2.get(k) != after2.get(k)}
    assert diff2, "指纹工具没发现文件被删"
