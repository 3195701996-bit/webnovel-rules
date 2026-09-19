# -*- coding: utf-8 -*-
"""R78 回归：跨源内容巡检的**判定逻辑**（工具 tools/probe_novel_content_parity.py）

为什么要给工具写单测：这套判定是我用来发现"站点给了错内容"的手段，
它自己出现"假绿灯"就会让我（和后续排查的人）以为某个源没问题。
本轮就踩了两次：

  1. 按"章名核心"对齐时**没区分重复标题**：实测精华书阁《剑来》尾部
     idx≥1226 是"目录重启块"（第6章 老酒、第14章 野草…与开头章名重复），
     不区分次序就把"开头的正常章"与"尾部重启块"混成一组，把差异平均掉 → 漏报；
  2. 取数失败的源被判 **"✓ 正常（共 0 章）"** ——没有数据却给出绿灯，
     正是本仓库一直在修的那类假绿灯。

判据：
  · 明显偏短（<0.7× 中位）必须报出；
  · 重复标题按"第几次出现"分别比对（第二次出现的那组单独判定）；
  · **没有样本的源不得判"正常"**，必须出现在 no_data 里、结论为"无结论"；
  · 覆盖不足（可比源 <3）的标题单独计数，不混进"可疑 0 条"的假安心。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

from probe_novel_content_parity import analyze      # noqa: E402


def _row(samples, chapters=100, rng=(1, 100)):
    return {"chapters": chapters, "range": list(rng), "samples": samples,
            "stage": "", "reason": ""}


def _s(chars, name="第一千二百章 某章"):
    return {"chars": chars, "name": name, "head": "头", "tail": "尾"}


def test_short_outlier_is_flagged():
    """站点把正文换成别的书（实测 0.07~0.26×）必须报出"""
    rows = {
        "good1": _row({"某章#1": _s(10000)}),
        "good2": _row({"某章#1": _s(10200)}),
        "good3": _row({"某章#1": _s(9800)}),
        "poisoned": _row({"某章#1": _s(1600)}),
    }
    res = analyze(rows)
    assert len(res["suspects"]) == 1, res["suspects"]
    sp = res["suspects"][0]
    assert sp["uid"] == "poisoned" and sp["why"] == "明显偏短"
    assert res["no_data"] == []


def test_long_outlier_is_flagged():
    """跟随分页链接走出本章（把后续章节拼进来）也要报出"""
    rows = {
        "a": _row({"某章#1": _s(6000)}), "b": _row({"某章#1": _s(6100)}),
        "c": _row({"某章#1": _s(5900)}), "spill": _row({"某章#1": _s(26000)}),
    }
    res = analyze(rows)
    assert [s["uid"] for s in res["suspects"]] == ["spill"]
    assert res["suspects"][0]["why"] == "明显偏长"


def test_duplicate_titles_are_compared_separately():
    """重复标题按"第几次出现"分组：尾部重启块不能与开头章混算

    实测形态：开头"第6章 老酒"（正常长）与尾部重启块"第6章 老酒"（另一处）。
    若不区分次序，两组数值混在一起中位被拉平 → 污染漏报。
    """
    rows = {
        "ref": _row({"老酒#1": _s(4200), "老酒#2": _s(4200)}),
        "b": _row({"老酒#1": _s(4300), "老酒#2": _s(4250)}),
        "c": _row({"老酒#1": _s(4100), "老酒#2": _s(4150)}),
        # 该源只在"第二次出现"处给了错内容
        "bad": _row({"老酒#1": _s(4200), "老酒#2": _s(1205)}),
    }
    res = analyze(rows)
    sus = [s for s in res["suspects"] if s["uid"] == "bad"]
    assert len(sus) == 1 and sus[0]["title"].endswith("#2"), res["suspects"]
    # 若两者混算（旧行为）中位≈4200、短值 1205 仍可疑——但**正常那组会被误伤**；
    # 这里额外断言 #1 组无人被判可疑
    assert not [s for s in res["suspects"] if s["title"].endswith("#1")]


def test_source_without_samples_is_not_reported_as_ok():
    """**关键**：没有正文样本的源必须进 no_data，不能被算成"正常" """
    rows = {
        "ok1": _row({"某章#1": _s(5000)}), "ok2": _row({"某章#1": _s(5100)}),
        "ok3": _row({"某章#1": _s(4900)}),
        "dead": _row({}, chapters=0, rng=(0, 0)),
        "failed": {"chapters": 100, "range": [1, 100], "stage": "目录",
                   "reason": "DeadlineExceeded",
                   "samples": {"某章#1": {"error": "Timeout", "name": "某章"}}},
    }
    res = analyze(rows)
    assert set(res["no_data"]) == {"dead", "failed"}, res["no_data"]
    # 判定表里这两条 got_n 必须为 0（report() 据此打"无结论"）
    for bad, uid, _c, _r, got_n, _e in res["verdict"]:
        if uid in ("dead", "failed"):
            assert got_n == 0


def test_thin_coverage_is_counted_not_silently_ok():
    """只比了 1~2 个源的标题判定不了 → 单独计数，避免"0 可疑"被当成"全正常" """
    rows = {"a": _row({"孤章#1": _s(1000)}), "b": _row({"孤章#1": _s(9000)})}
    res = analyze(rows)
    assert res["suspects"] == [], "两个源之间不判定（中位无意义）"
    assert res["thin_titles"] == 1 and res["covered_titles"] == 1
