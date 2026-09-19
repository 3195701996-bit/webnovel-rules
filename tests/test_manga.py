# -*- coding: utf-8 -*-
"""漫画板块测试"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

def test_manga_base_models():
    from engine.manga.base import Comic, Chapter, ComicDetails
    c = Comic(id="x", title="测试")
    assert c.id == "x" and c.title == "测试"
    ch = Chapter(id="c1", name="第1话")
    assert ch.name == "第1话"
    d = ComicDetails(id="x", title="测试", chapters=[ch])
    assert len(d.chapters) == 1
