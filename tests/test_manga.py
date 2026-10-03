# -*- coding: utf-8 -*-
"""漫画板块测试"""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_shared_chapter_order_fixture_matches_server_canonical_catalog():
    from engine.manga.download_manager import _is_volume_only, _sort_chapters

    fixture = (
        Path(__file__).resolve().parents[1]
        / "android/app/src/test/resources/manga_chapter_order_parity.json"
    )
    cases = json.loads(fixture.read_text(encoding="utf-8"))["cases"]
    for case in cases:
        sorted_rows = _sort_chapters(case["chapters"])
        volumes = [row for row in sorted_rows if _is_volume_only(row["name"])]
        episodes = [row for row in sorted_rows if not _is_volume_only(row["name"])]
        actual = [row["id"] for row in volumes + episodes]
        assert actual == case["expected"], case["name"]


def test_shared_manga_identity_fixture_matches_server_alias_contract():
    from server.state import manga_identity_source, manga_source_aliases

    fixture = (
        Path(__file__).resolve().parents[1]
        / "android/app/src/test/resources/manga_identity_parity.json"
    )
    cases = json.loads(fixture.read_text(encoding="utf-8"))["cases"]
    for case in cases:
        transport = case["transport"]
        assert manga_identity_source(transport) == case["identity"], case["name"]
        assert list(manga_source_aliases(transport)) == case["aliases"], case["name"]

def test_sort_chapters():
    from engine.manga.download_manager import _sort_chapters
    chs = [
        {"id": "1", "name": "第1186话 再一次"},
        {"id": "2", "name": "第2话 路飞"},
        {"id": "3", "name": "第1话 ROMANCE"},
        {"id": "4", "name": "番外篇"},
    ]
    s = _sort_chapters(chs)
    assert s[0]["name"].startswith("第1话")
    assert s[1]["name"].startswith("第2话")
    assert s[-1]["name"] == "番外篇"

def test_sort_chapters_volume():
    from engine.manga.download_manager import _sort_chapters
    chs = [{"id": "1", "name": "Vol.1 第3话"}, {"id": "2", "name": "第2话"}]
    s = _sort_chapters(chs)
    # 话号主排序：第2话 < Vol.1第3话
    assert s[0]["name"] == "第2话"
    assert s[1]["name"] == "Vol.1 第3话"


def test_sort_chapters_chinese_numeral_volumes_and_episodes():
    from engine.manga.download_manager import _sort_chapters
    chapters = [
        {"id": "v12", "name": "第十二卷"},
        {"id": "c2", "name": "第2話"},
        {"id": "v3", "name": "第三卷"},
        {"id": "c1", "name": "第1話"},
    ]
    ordered = _sort_chapters(chapters)
    assert [item["id"] for item in ordered] == ["c1", "c2", "v3", "v12"]


def test_sort_chapters_embedded_serial():
    """JM 式"标题+序号"命名（无"第X话"标记）：序号当正片话号，不沉底。
    2026-09-27 实测回归：'缺德鄰居難相處50 …' 被排到第100話之后"""
    from engine.manga.download_manager import _sort_chapters
    chs = [
        {"id": "1", "name": "缺德鄰居麥相害 第100話"},
        {"id": "2", "name": "缺德鄰居難相處50 缺德鄰居難相處(50)"},
        {"id": "3", "name": "缺德鄰居麥相害 第87話"},
    ]
    s = _sort_chapters(chs)
    assert s[0]["name"].startswith("缺德鄰居難相處50")
    assert s[1]["name"].endswith("第87話")
    assert s[2]["name"].endswith("第100話")


def test_sort_chapters_hyphen_segment():
    """连字符分段序号："37-1"=37.1，排在 36.5 与 37.2 之间，不得沉底或置顶。
    2026-09-27 实测回归：旧逻辑抠出尾段"1"当话号 → 排到第1話前面"""
    from engine.manga.download_manager import _sort_chapters
    chs = [
        {"id": "1", "name": "37-1"},
        {"id": "2", "name": "37.2"},
        {"id": "3", "name": "36.5"},
        {"id": "4", "name": "第1話"},
    ]
    s = _sort_chapters(chs)
    assert [c["name"] for c in s] == ["第1話", "36.5", "37-1", "37.2"]


def test_sort_chapters_tankobon_appendix_is_extra():
    """單本附錄是附加内容：'單本4 附錄-1' 不得插进第3話/第4話中间"""
    from engine.manga.download_manager import _sort_chapters
    chs = [
        {"id": "1", "name": "單本4 附錄-1"},
        {"id": "2", "name": "第4話"},
        {"id": "3", "name": "第3話"},
        {"id": "4", "name": "單本4 附錄-2"},
    ]
    s = _sort_chapters(chs)
    assert [c["name"] for c in s[:2]] == ["第3話", "第4話"]
    assert {c["name"] for c in s[2:]} == {"單本4 附錄-1", "單本4 附錄-2"}


def test_sort_chapters_extra_keywords_still_last():
    """附加类命名里的数字不能当话号（防回归：特別篇2 误插正片中间）"""
    from engine.manga.download_manager import _sort_chapters
    chs = [
        {"id": "1", "name": "第3话"},
        {"id": "2", "name": "特別篇2"},
        {"id": "3", "name": "第1话"},
        {"id": "4", "name": "休載公告1"},
    ]
    s = _sort_chapters(chs)
    assert [c["name"] for c in s[:2]] == ["第1话", "第3话"]
    assert {c["name"] for c in s[2:]} == {"特別篇2", "休載公告1"}

def test_manga_base_models():
    from engine.manga.base import Comic, Chapter, ComicDetails
    c = Comic(id="x", title="测试")
    assert c.id == "x" and c.title == "测试"
    ch = Chapter(id="c1", name="第1话")
    assert ch.name == "第1话"
    d = ComicDetails(id="x", title="测试", chapters=[ch])
    assert len(d.chapters) == 1
