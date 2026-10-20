# -*- coding: utf-8 -*-
"""R79 回归：失败章节数必须**以磁盘为准**（`_state.json` 的 failed 可能是旧记录）。

实测缺陷（用户真实书库）：《在美漫当心灵导师的日子》5035 章里有 **2 章**
`_state.json` 仍挂着失败原因 `RuntimeError: 爱下书正文容器缺失`，
而它们的缓存文件**存在且是 1 万字正常正文**（能正常阅读）✗。
后果：详情接口报 `failed_count: 2`，**检查更新会劝用户再下一次已经能读的章**。

判据：`cache_key_of(url) + ".cache"` 存在且非空 → 不算失败（与下载路径写缓存的口径一致）。
本文件锁死两条：① 详情接口的 failed_count；② 检查更新不再把这类章算进缺失。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.app_utils import cache_key_of          # noqa: E402


def _make_book(tmp_book_dir, with_cache):
    os.makedirs(tmp_book_dir, exist_ok=True)
    chs = [{"name": f"第{i}章", "url": f"https://t.test/read/1/{i}.html"}
           for i in range(1, 4)]
    for c in chs[:2]:
        with open(os.path.join(tmp_book_dir, cache_key_of(c["url"]) + ".cache"), "w",
                  encoding="utf-8") as f:
            f.write("第%s章\n\n正文内容。" % c["name"])
    if with_cache:                       # 第 3 章也写缓存，但 state 里仍标成失败
        with open(os.path.join(tmp_book_dir, cache_key_of(chs[2]["url"]) + ".cache"), "w",
                  encoding="utf-8") as f:
            f.write("第三章\n\n这一章其实已经拿到了正文，不该再算失败。")
    with open(os.path.join(tmp_book_dir, "_state.json"), "w", encoding="utf-8") as f:
        json.dump({"book": {"name": "口径测试书", "author": "作者", "source_uid": "src",
                            "book_url": "https://t.test/read/1/"},
                   "chapters": chs, "completed": [c["url"] for c in chs[:2]],
                   "failed": {chs[2]["url"]: "正文容器缺失（旧记录）"}},
                  f, ensure_ascii=False)
    return chs


def test_failed_count_ignores_chapters_with_cache(tmp_path):
    import server.novel_api as na
    d = os.path.join(na.BOOKS_DIR, "src_live")
    chs = _make_book(d, with_cache=True)
    live = na._failed_live(d, {chs[2]["url"]: "旧记录"})
    assert live == {}, f"有正文的章仍被算作失败：{live}"


def test_failed_count_keeps_truly_missing(tmp_path):
    import server.novel_api as na
    d = os.path.join(na.BOOKS_DIR, "src_dead")
    chs = _make_book(d, with_cache=False)
    live = na._failed_live(d, {chs[2]["url"]: "真的没拿到"})
    assert list(live) == [chs[2]["url"]], f"真正缺失的章被吞掉了：{live}"


def test_detail_recovered_chapter_has_no_stale_failure():
    import app
    import server.novel_api as na
    d = os.path.join(na.BOOKS_DIR, "src_recovered_detail")
    _make_book(d, with_cache=True)
    body = app.app.test_client().get("/api/books/src_recovered_detail").get_json()
    assert body["failed_count"] == 0
    assert body["chapters"][2]["downloaded"] is True
    assert body["chapters"][2]["failed"] is False
    assert body["chapters"][2]["failed_reason"] == ""


def test_shelf_and_detail_failure_counts_follow_same_disk_truth():
    import app
    import server.novel_api as na
    key = "src_shelf_recovered"
    directory = os.path.join(na.BOOKS_DIR, key)
    chapters = _make_book(directory, with_cache=True)
    client = app.app.test_client()
    for expected in (0, 1):
        if expected:
            os.remove(os.path.join(directory, cache_key_of(chapters[2]["url"]) + ".cache"))
        detail = client.get(f"/api/books/{key}").get_json()
        shelf = client.get("/api/books").get_json()["books"]
        book = next(row for row in shelf if row["key"] == key)
        assert book["failed"] == detail["failed_count"] == expected
