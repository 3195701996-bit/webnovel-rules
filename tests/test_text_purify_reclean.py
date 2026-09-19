# -*- coding: utf-8 -*-
"""0.60.0 回归（离线）：正文净化补强 + "已下载内容"的重新净化。

用户反馈（原话）："以已导出的漫威心灵导师为例，里面存在大量的句末重复无意义生僻字
和广告如'换源'等"。对着**真实文件**取样后确认了两类污染：

  1. `【新章节更新迟缓的问题，在能换源的app上终于有了解决之道，这里下载huanyuanap】`
     —— 关键词里夹了"的"、下载域名写成 `huanyuanap`（单 p），旧规则（`换源app`、
     `huanyuanapp`）**都匹配不到**；
  2. `小书亭app` / `笔趣书阁` 这类"整行就是 App 名/站名"的广告；
  3. 纯 `─` 分隔线（实测该书导出文件里有 **5014 行**）——缓存里已经没有了，
     但**导出的 book.txt 是旧的**：净化改进了，导出文件不会自动跟着更新。

因此本文件锁两件事：

  A. 净化规则：上面两类必须清掉，同时**不能误伤作者正文**
     （"全都给我去下国家反诈APP！！！" 是作者原话，必须保留）；
  B. `POST /api/books/<key>/reclean`：能把**已经落盘**的章节缓存重新过一遍管线，
     先给 dry-run 数字、再真改；数字必须分得清"整行垃圾"与"行内改写"，
     且**幂等**（跑第二遍 0 改动），并重建全文导出。
"""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.cleaner import clean_text  # noqa: E402

# 真实样本（取自用户导出的《在美漫当心灵导师的日子》book.txt 与章节缓存）
REAL_ADS = [
    '【新章节更新迟缓的问题，在能换源的app上终于有了解决之道，这里下载huanyuanap】',
    '【新章节更新迟缓的问题，在能换源的app上终于有了解决之道，这里下载huanyuanapp】',
    '小书亭app',
    '笔趣书阁',
    '言情小说吧免费阅读',
]
# 作者正文 / 正常句子：净化**必须保留**
MUST_KEEP = [
    '全都给我去下国家反诈APP！！！',
    '我打开了那个app看了看，觉得还行。',
    '他说：“这个 app 挺好用。”',
    '席勒重新拿起餐具，开始切割盘子上的食物，他说：“如果你对这个问题很好奇，那我们就先来谈谈西蓝花的问题。”',
]


# ── A. 净化规则 ───────────────────────────────────────────────────
@pytest.mark.parametrize("line", REAL_ADS)
def test_real_ad_lines_are_cleaned(line):
    assert clean_text(line).strip() == "", f"这条广告必须被清掉：{line!r}"


@pytest.mark.parametrize("line", MUST_KEEP)
def test_author_text_is_never_eaten(line):
    assert clean_text(line).strip() == line.strip(), f"正文被误删：{line!r}"


def test_standalone_title_line_is_removed_by_design():
    """整行章节标题属于"正文内标题残留"（分页站点会重复输出），净化按设计删除。
    缓存级另有保护：写回时**保留缓存首行标题**（见 reclean 用例）。"""
    assert clean_text("第一章 钱的问题").strip() == ""


def test_separator_lines_variants():
    for line in ('─' * 40, '── ─ ──', '━' * 10, '═══', '···', '※※※'):
        assert clean_text(line).strip() == "", f"分隔线必须清掉：{line!r}"


def test_separator_inside_text_keeps_content():
    out = clean_text("第一段正文。\n" + "─" * 20 + "\n第二段正文。")
    assert "第一段正文" in out and "第二段正文" in out
    assert "─" not in out


def test_inline_ad_removed_but_sentence_kept():
    """行内广告摘掉，句子本身留下（不能整句删）"""
    src = "他看着窗外，忽然想起【新章节更新迟缓的问题，在能换源的app上终于有了解决之道，这里下载huanyuanap】于是起身走了出去。"
    out = clean_text(src)
    assert "huanyuanap" not in out and "换源" not in out
    assert "他看着窗外" in out and "于是起身走了出去" in out


def test_cleaning_is_idempotent_on_clean_text():
    clean = ("席勒重新拿起餐具，开始切割盘子上的食物。\n"
             "“如果你对这个问题很好奇，那我们就先来谈谈西蓝花的问题。”")
    assert clean_text(clean_text(clean)).strip() == clean_text(clean).strip()


# ── B. reclean：已落盘内容的重新净化 ───────────────────────────────
@pytest.fixture(scope="module")
def client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


@pytest.fixture()
def book(monkeypatch, tmp_path):
    """造一本"老缓存里带垃圾"的书：3 章缓存（1 章干净、2 章带广告与分隔线）"""
    import server.state as st
    from server.novel_api import BOOKS_DIR as API_BOOKS_DIR
    from engine.app_utils import cache_key_of

    books = tmp_path / "books"
    books.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(st, "BOOKS_DIR", str(books), raising=False)
    monkeypatch.setattr("server.novel_api.BOOKS_DIR", str(books), raising=False)
    assert API_BOOKS_DIR  # 存在性检查（import 失败会在这里暴露）
    key = "reclean_src_deadbeef"
    d = books / key
    d.mkdir(parents=True, exist_ok=True)
    chs = [{"name": f"第{i}章 测试", "url": f"https://t.example.com/b/{i}"} for i in (1, 2, 3)]
    with open(str(d / "_state.json"), "w", encoding="utf-8") as f:
        json.dump({"book": {"name": "净化测试书", "source_uid": "reclean_src",
                            "book_url": "https://t.example.com/b/"},
                   "chapters": chs,
                   "completed": [c["url"] for c in chs], "failed": {}}, f, ensure_ascii=False)
    bodies = {
        1: "第一章 测试\n\n这一章本来就很干净，不该被改动。",
        2: ("第二章 测试\n\n正文第一段，正常内容。\n"
            + "─" * 30 + "\n"
            "【新章节更新迟缓的问题，在能换源的app上终于有了解决之道，这里下载huanyuanap】\n"
            "小书亭app\n正文第二段，也要保留。"),
        3: ("第三章 测试\n\n全都给我去下国家反诈APP！！！\n"
            "他打开了那个app看了看。\n"),
    }
    for i, body in bodies.items():
        (d / (cache_key_of(chs[i - 1]["url"]) + ".cache")).write_text(body, encoding="utf-8")
    st.invalidate_book_state(str(d / "_state.json"))
    yield key, d, chs, bodies
    shutil.rmtree(str(books), ignore_errors=True)


def test_reclean_dry_run_reports_without_touching_files(client, book):
    key, d, chs, bodies = book
    r = client.post(f"/api/books/{key}/reclean?dry_run=1")
    assert r.status_code == 200
    o = r.get_json()
    assert o["dry_run"] is True and o["chapters"] == 3
    # 只有第 2 章带垃圾；第 1 章本来就干净、第 3 章是"作者正文"（含 app 字样但不该改）
    assert o["changed"] == 1, f"只有第 2 章需要净化（实际 {o['changed']}）"
    assert o["already_clean"] == 2
    # 整行垃圾：第 2 章的分隔线 + 广告句 + 小书亭app 共 3 行
    assert o["removed_lines"] == 3, o
    assert o["removed_chars"] > 0
    assert o["samples"], "必须给出被删内容样例（用户要核对删了什么）"
    assert o["txt_rebuilt"] is False, "dry-run 不许改任何东西"
    # 文件一个字都没动
    from engine.app_utils import cache_key_of
    p2 = d / (cache_key_of(chs[1]["url"]) + ".cache")
    assert p2.read_text(encoding="utf-8") == bodies[2]
    p3 = d / (cache_key_of(chs[2]["url"]) + ".cache")
    assert p3.read_text(encoding="utf-8") == bodies[3]


def test_reclean_rewrites_caches_and_keeps_author_text(client, book):
    key, d, chs, bodies = book
    from engine.app_utils import cache_key_of
    r = client.post(f"/api/books/{key}/reclean", json={"dry_run": False})
    assert r.status_code == 200
    o = r.get_json()
    assert o["dry_run"] is False and o["changed"] == 1 and o["failed"] == []

    p2 = d / (cache_key_of(chs[1]["url"]) + ".cache")
    t2 = p2.read_text(encoding="utf-8")
    assert "──────" not in t2 and "huanyuanap" not in t2 and "小书亭" not in t2
    assert "正文第一段" in t2 and "正文第二段" in t2, "正文段落不能被删"
    assert t2.startswith("第二章 测试"), "章节标题必须保留（净化不碰标题行）"

    p3 = d / (cache_key_of(chs[2]["url"]) + ".cache")
    t3 = p3.read_text(encoding="utf-8")
    assert "全都给我去下国家反诈APP！！！" in t3, "作者原话必须保留"
    assert "他打开了那个app看了看。" in t3

    # 干净的那章不许被改写（避免无谓写盘/指纹变化）
    p1 = d / (cache_key_of(chs[0]["url"]) + ".cache")
    assert p1.read_text(encoding="utf-8") == bodies[1]


def test_reclean_is_idempotent(client, book):
    key, d, chs, bodies = book
    client.post(f"/api/books/{key}/reclean", json={"dry_run": False})
    r2 = client.post(f"/api/books/{key}/reclean?dry_run=1").get_json()
    assert r2["changed"] == 0, "跑第二遍必须 0 改动（净化是幂等的）"
    assert r2["removed_lines"] == 0 and r2["removed_chars"] == 0


def test_reclean_missing_book_is_404(client, book):
    r = client.post("/api/books/no_such_book_zzz/reclean")
    assert r.status_code == 404


def test_reclean_reports_truthful_totals(client, book):
    """释放量与实际差异必须对得上（不夸大也不藏）"""
    key, d, chs, bodies = book
    from engine.app_utils import cache_key_of
    r = client.post(f"/api/books/{key}/reclean?dry_run=1").get_json()
    diff = 0
    for i in (1, 2, 3):
        _p = d / (cache_key_of(chs[i - 1]["url"]) + ".cache")
        body = bodies[i]
        head, _s, rest = body.partition("\n")
        cleaned = clean_text(rest)
        if cleaned.strip() == rest.strip():
            continue          # 未改动的章节不计入（与端点同一判据）
        diff += max(0, len(rest) - len(cleaned))
    assert r["removed_chars"] == diff, f"报的 {r['removed_chars']} 与实际 {diff} 不符"


# ── R77（2026-09-18 逐源实测抓到的两类残留）────────────────────────────
# 来源：tools/probe_novel_sources.py --deep 对 16 个手机口径源抽"中段一章"检查，
# 在 精华书阁（夹在段落中间的换域名广告）与 啃书网（章节开头的导语推广）各抓到一处。
# 两条都要求"站方口吻词 + 动作词"同时出现，反例守护在下面。

def test_site_redirect_ad_inside_paragraph_is_removed():
    """换域名广告**夹在正文段落中间**（无换行）：实测原文

    「…相传为一位菩萨的悟道最快更新请浏览器输入-JHSSD.COM-到新笔趣阁进行查看可当陈平安…」
    """
    from engine.cleaner import clean_text
    raw = ("有一块大石，相传为一位菩萨的悟道"
           "最快更新请浏览器输入-JHSSD.COM-到新笔趣阁进行查看"
           "可当陈平安问了好几个人，竟然人人都说不知什么如去寺")
    out = clean_text(raw)
    assert "JHSSD" not in out and "新笔趣阁" not in out and "浏览器输入" not in out, out
    assert "相传为一位菩萨的悟道" in out, "广告前后的正文必须保留：%s" % out
    assert "可当陈平安问了好几个人" in out, "广告前后的正文必须保留：%s" % out


def test_promo_lead_line_is_removed():
    """章节开头的导语推广：实测原文「一秒记住♂ ，更新快，，免费读！」"""
    from engine.cleaner import clean_text
    raw = "一秒记住♂ ，更新快，，免费读！\n\n新书的重心在于构建一个光怪陆离的仙侠世界"
    out = clean_text(raw)
    assert "一秒记住" not in out and "免费读" not in out, out
    assert "新书的重心在于" in out, out


def test_normal_prose_with_similar_words_is_kept():
    """反例：正文里的普通叙述不得被误删（正则必须要求"站方口吻+动作词"同现）"""
    from engine.cleaner import clean_text
    for raw in ("他输入密码查看结果，发现一切正常。",
                "陈平安一秒钟记住了一百个字。",
                "她快速更新了名单，然后查看了一遍。"):
        out = clean_text(raw)
        assert raw.replace("，", "，") in out or len(out) >= len(raw) - 2, (
            "疑似误伤正文：%r → %r" % (raw, out))


# ── C. 0.74.14：本轮实测抓到的那几类垃圾，也必须能被 reclean 清掉 ──────────
# 背景：用户在手机上的动作是"设置 → 那本书 → 重新净化"。本轮新增的判据
# （整行 JUNK_LINE_RES / 行内 INLINE_JUNK_RES / L6 家族词）如果只在**新抓取**
# 生效、对**已落盘缓存**不生效，用户就得删书重下——所以这条路径要单独锁。
MEASURED_JUNK_BODIES = {
    # 实测原样（思路客第131章）：垃圾**粘在正文行尾**而不是独立行
    1: ("第一百三十一章 书生弟子\n\n"
        "傅玉叹了口气。吴鸢好像自言自语道。\n"
        "、更新快,会员同步书架,请关注\xa0gegegengxin\xa0(按住三秒复制)\xa0下载免费阅读器!!  "
        "章节错误,点此报送(免注册), 报送后维护人员会在两分钟内校正章节内容,请耐心等待。\n"
        "read3();\n"
        "本站重要通知:请使用本站的免费小说APP,无广告、破防盗版、"),
    # 实测原样（大帝书阁第130章）：JSON 残块 + JS 残句
    2: ("第一百三十章 山水少年\n\n"
        "杨老头犟不过，只好答应\n"
        "\",\"message\":\"已经订阅 先定个小目标，比如1秒记住：书客居手机版阅读网址：\n"
        "cambrian.render('body')\n('body')"),
}
MEASURED_JUNK_WORDS = ("按住三秒复制", "meinvmei", "报送后维护人员", "read3();",
                       "已经订阅", "先定个小目标", "书客居", "cambrian",
                       "免费小说APP", "破防盗版")


@pytest.fixture()
def junk_book(monkeypatch, tmp_path):
    """一本"老缓存里带本轮实测垃圾"的书（2 章带垃圾 + 1 章干净对照）"""
    import server.state as st
    from engine.app_utils import cache_key_of

    books = tmp_path / "books2"
    books.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(st, "BOOKS_DIR", str(books), raising=False)
    monkeypatch.setattr("server.novel_api.BOOKS_DIR", str(books), raising=False)
    key = "isiluke_deadbeef"
    d = books / key
    d.mkdir(parents=True, exist_ok=True)
    chs = [{"name": f"第{i}章 测试", "url": f"https://www.isiluke.la/read/1/{i}.html"}
           for i in (1, 2, 3)]
    with open(str(d / "_state.json"), "w", encoding="utf-8") as f:
        json.dump({"book": {"name": "剑来", "source_uid": "思路客_isiluke_la__www.isiluke.la",
                            "book_url": "https://www.isiluke.la/read/1/"},
                   "chapters": chs, "completed": [c["url"] for c in chs], "failed": {}},
                  f, ensure_ascii=False)
    clean = "第三章 测试\n\n这一章本来就很干净，不该被改动一个字符。"
    for i, body in MEASURED_JUNK_BODIES.items():
        (d / (cache_key_of(chs[i - 1]["url"]) + ".cache")).write_text(body, encoding="utf-8")
    (d / (cache_key_of(chs[2]["url"]) + ".cache")).write_text(clean, encoding="utf-8")
    st.invalidate_book_state(str(d / "_state.json"))
    yield key, d, chs, clean
    shutil.rmtree(str(books), ignore_errors=True)


def test_reclean_removes_measured_junk_from_disk(client, junk_book):
    """实测垃圾必须能从**已落盘**的缓存里清掉（不是只对新抓取生效）"""
    key, d, chs, clean = junk_book
    r = client.post(f"/api/books/{key}/reclean", json={"dry_run": False})
    j = r.get_json()
    assert j["changed"] == 2 and not j["failed"], j

    from engine.app_utils import cache_key_of
    for i in (1, 2):
        txt = (d / (cache_key_of(chs[i - 1]["url"]) + ".cache")).read_text(encoding="utf-8")
        hits = [w for w in MEASURED_JUNK_WORDS if w in txt]
        assert not hits, f"第{i}章仍有残留 {hits}"
        # 缓存首行标题不参与净化（否则 L3 会把标题本身删掉）
        assert txt.split("\n")[0].strip() == MEASURED_JUNK_BODIES[i].split("\n")[0].strip(), \
            "缓存标题行被破坏"
    t1 = (d / (cache_key_of(chs[0]["url"]) + ".cache")).read_text(encoding="utf-8")
    assert "傅玉叹了口气" in t1, "正文被误删"
    t2 = (d / (cache_key_of(chs[1]["url"]) + ".cache")).read_text(encoding="utf-8")
    assert "杨老头犟不过" in t2, "正文被误删"
    # 干净章必须逐字不变（reclean 只动需要动的）
    t3 = (d / (cache_key_of(chs[2]["url"]) + ".cache")).read_text(encoding="utf-8")
    assert t3 == clean, "干净章节被改动"


def test_reclean_on_measured_junk_is_idempotent(client, junk_book):
    key, _d, _chs, _clean = junk_book
    assert client.post(f"/api/books/{key}/reclean",
                       json={"dry_run": False}).get_json()["changed"] == 2
    second = client.post(f"/api/books/{key}/reclean", json={"dry_run": False}).get_json()
    assert second["changed"] == 0, f"不幂等：第二遍仍在改 {second}"
    assert second["already_clean"] == 3, second
