# -*- coding: utf-8 -*-
"""R78 回归：净化器必须删掉**实测抓到**的适配器源正文残留。

背景（2026-09-18 跨源等长普查，见 报告归档/03-验证结果/）：
把下列四段真实残留喂给 clean_text，**旧净化输出与输入等长**（一条都没删）——
读者会在大段正文里读到别的站名、色情引流和 JS 残句：

  · 思路客（isiluke）章尾：色情引流行 + "章节错误,点此报送(免注册), 报送后维护人员
    会在两分钟内校正章节内容,请耐心等待。" + "read3();"
  · 大帝书阁（ddsk）章尾：`","message":"已经订阅 先定个小目标，比如1秒记住：
    书客居手机版阅读网址：` + `cambrian.render('body')` + `('body')`
  · 精华书阁（jhsssd）："阅读提示：为防止内容获取不全，请勿使用浏览器阅读模式。"

判据两条，缺一不可：
  1. 这些垃圾**必须被删掉**（按整行删，它们是独立行）；
  2. **正文一句都不能碰**——尤其"那位女主播走进了房间。""章节错误是作者笔下的
     书名。"这类含关键词的正常句子（第一版判据就差点误删，故单独锁死）。
"""
from engine.cleaner import clean_text, check_purify_quality


ISILUKE_TAIL = (
    "行。有山时看山，有水时听水。\n"
    "泰国最胸女主播全新激_情视频曝光 扑倒男主好饥_渴!!请关注 meinvmei222 (长按三秒复制)!!\n"
    "章节错误,点此报送(免注册), 报送后维护人员会在两分钟内校正章节内容,请耐心等待。\n"
    "read3();"
)
DDSK_TAIL = (
    "杨老头犟不过，只好答应\n"
    "\",\"message\":\"已经订阅 先定个小目标，比如1秒记住：书客居手机版阅读网址：\n"
    "cambrian.render('body')\n"
    "('body')"
)
JHSSSD_TAIL = "爱不释手。\n阅读提示：为防止内容获取不全，请勿使用浏览器阅读模式。"


def test_isiluke_tail_junk_removed():
    out = clean_text(ISILUKE_TAIL)
    assert "有山时看山，有水时听水。" in out, "正文被误删"
    for junk in ("女主播", "长按三秒复制", "meinvmei", "章节错误",
                 "报送后维护人员", "read3()"):
        assert junk not in out, f"残留未清除：{junk}｜输出={out!r}"


def test_ddsk_tail_junk_removed():
    out = clean_text(DDSK_TAIL)
    assert "杨老头犟不过，只好答应" in out, "正文被误删"
    for junk in ("message", "已经订阅", "先定个小目标", "书客居",
                 "cambrian", "body"):
        assert junk not in out, f"残留未清除：{junk}｜输出={out!r}"


def test_jhsssd_reading_hint_removed():
    out = clean_text(JHSSSD_TAIL)
    assert "爱不释手。" in out
    assert "阅读提示" not in out and "浏览器阅读模式" not in out, out


def test_prose_with_keywords_is_untouched():
    """**误伤边界**：含关键词的正常正文一句都不能删"""
    prose = (
        "那位女主播走进了房间。\n"
        "他订阅了三年的报纸。\n"
        "请到官网注册账号后再试。\n"
        "章节错误是作者笔下的书名。\n"
        "李三说：这件事我一定要举报他。\n"
        "读3(); 这行不是代码，是排版事故。"
    )
    assert clean_text(prose) == prose, "正常正文被误删"


def test_quality_checker_flags_these_residues():
    """质量自检要能抓到这几类——否则"逐源实测"会把脏正文报成没问题"""
    for junky in ("正文……长按三秒复制", "正文……请勿使用浏览器阅读模式",
                  "正文……已经订阅", "正文……cambrian.render"):
        ok, probs = check_purify_quality(junky * 20)
        assert not ok and any("广告" in p or "导航" in p for p in probs), (junky, probs)


def test_cleaned_measured_chapter_passes_quality_check():
    """净化后的实测章尾应通过质量自检（闭环：删得干净才算修好）"""
    body = ("他站在山巅，看云海翻涌。" * 40) + "\n" + ISILUKE_TAIL + "\n" + DDSK_TAIL
    out = clean_text(body)
    ok, probs = check_purify_quality(out)
    assert ok, f"净化后仍被判为脏：{probs}"


# ── 同一族的**轮换变体**（站点会换措辞，必须按家族判据而不是逐条抄）──────
AD_VARIANTS = (
    "本站重要通知:请使用本站的免费小说APP,无广告、破防盗版、",
    "本站重要通知：请使用本站的免费小说APP",
    "请使用本站的免费小说APP阅读最新章节",
    "无广告、破防盗版、更新快",
    "看正版小说请到本站，无广告弹窗",
)


def test_ad_variant_family_is_removed():
    """实测：思路客同一章先后出现三种措辞。按家族判据（L6 强特征词）覆盖"""
    for v in AD_VARIANTS:
        assert not clean_text(v).strip(), f"残留：{v!r}"


def test_ad_family_does_not_eat_prose():
    """**误伤边界**：含"本站/APP/正版/防盗版/无广告"的正常句子必须原样保留
    （第一版把 `请到本站` 当强特征词，`请到本站的论坛发帖。` 被整句删掉）"""
    prose = (
        "他在APP上看了新闻。",
        "本站不支持转载，请见谅。",
        "这本书我看的是正版。",
        "防盗版的水印很淡，几乎看不见。",
        "请到本站的论坛发帖。",
        "无广告的阅读体验很好。",
    )
    for p in prose:
        assert clean_text(p).strip() == p, f"正常正文被误删/改动：{p!r}"


# ── 质量自检的**误报**（0.74.17）：对白密集正文被当成"段落空白异常" ────────
def test_dialogue_heavy_chapter_passes_quality_check():
    """实测形态：精华书阁《剑来》第522章 14912 字 / **395 个 \\n\\n**，段落中位 29 字、
    连续空行 **0** 处——全是正常对白，却因旧判据（"\\n\\n 次数 > 字数//40"，
    即平均段长 < 40 字）被判"段落空白异常"。中文小说对白密集时平均段长本就
    26~37 字，这条判据必然误报；而它会被逐源实测写进"净化质量"给用户看。"""
    para = ["荆南国河流密布，两骑依旧是昼夜兼程。", "陈平安一掠而去。",
            "那座真正的战场。", "陈平安皱了皱眉头。", "一拳过后。", "宁姚相信他。"]
    # 放大到接近实测规模（~14.9k 字 / ~395 个空行），确认不再误报
    text = "\n\n".join(para * 300)          # ≈19.8k 字 / 1799 个空行
    assert len(text) > 12000, len(text)
    assert text.count("\n\n") > len(text) // 40, "本用例要覆盖'旧判据会触发'的区间"
    ok, probs = check_purify_quality(text)
    assert ok, f"正常对白正文被误判：{probs}"


def test_real_ad_slot_blank_lines_are_reported():
    """真正的空行广告位残留是"删掉广告行后留下的连续空行"（\\n\\n\\n）——必须报出"""
    body = ("正文第一段。" * 30)
    text = "\n\n\n".join([body] * 5) + "\n\n\n\n" + body
    ok, probs = check_purify_quality(text)
    assert not ok and any("空行" in p for p in probs), probs


def test_single_blank_line_separators_are_fine():
    """段间单个空行是正常排版，不能报（阈值给足余量）"""
    text = "\n\n".join(["这一段的正文内容大概就是这个样子，长度正常。" * 2] * 50)
    ok, probs = check_purify_quality(text)
    assert ok, f"正常分段被误判：{probs}"


# ── 2026-09-18 用户真实书库普查（6 本书 / 11695 章）补的一批 ──────────────
# 方法：统计"同一本书里跨章重复出现的短行"（正文不会重复，重复的基本都是站点
# 样板），再逐条喂给 clean_text 确认"当前确实清不掉"。实测这批一条都没被清掉。
# 下面每条都是**原文照抄**（含错别字/异体字/全角字符），不美化。
LIBRARY_JUNK = [
    # 夜天连看（yetianlian，现实书库里 1914 章）
    "小提示：按回车[]键返回书目，按←（键盘左键）返回上一章，按（键盘右键）→进入下一章。"
    "您的支持，就是我们最大的动力。",
    "小提示：按回车[]键返回书目，按←键返回上一页，按→键进入下一页。",
    "推荐一本好看的新作《狗神》，稳定更新，质量还不错。闹书荒的朋友可以看一下，呵呵。欢迎收藏订阅",
    # 爱下书（aixiashu，977 章）
    "一秒记住，為您提供精彩小说阅读。",
    "(.)u",
    # 我在龙族当老师（biqutu，560 章）
    "ｈttp://首发",
    "正在手打中，请稍等片刻，内容更新后，请重新刷新页面，即可获取最新更新！",
    "首发最新。",
    # 第二轮普查（清完上一批后再扫出来的）：UI 提示行 / 截断提示行 / JS 残句打头的推广块
    "已加入书签",
    "...手机用户请访问",
    "1();您的支持，就是我们最大的动力。斗破苍穹，无弹窗，免费阅读，TXT，无需注册，无需积分！"
    "斗破苍穹注册会员，就送书架！迷必备工具！",
    "1();2();3();",
    # 凡人修仙传（quanben8，2525 章）
    "(未完待续。如果您喜欢这部作品，欢迎您来起点（）投推荐票、月票，您的支持，就是我最大的动力。)",
]

# 行内形态：粘在正文行尾（整行判据删不掉），原文照抄
LIBRARY_INLINE = [
    ("女子眼角眉梢俱是媚意，只是假装楚楚可怜，怯生生的，没有急于扑入负笈生怀中。"
     "shouda8本章节狂人手打一秒记住本站。", "女子眼角眉梢俱是媚意"),
    ("思路上有些卡，需要些时间构思一下，向大家请假一晚！"
     "(未完待续。如果您喜欢这部作品，欢迎您来起点（）投推荐票、月票，您的支持，就是我最大的动力。)",
     "思路上有些卡"),
    # 转义 HTML 残渣 + 「记住本站」+ 「请在百度搜索」全粘在一行末尾
    ("曹长卿趁着徐凤年如同老僧入定，微微打量了几眼，是初入金刚境无疑，比较当初江南道初见，气象宏阔许多。"
     "一秒记住本站百度搜23文学网即可找到本站.fs23.{请在百度搜索1t;stronggt;1t;/stronggt;，全文字阅读}",
     "曹长卿趁着徐凤年如同老僧入定"),
    # 碎裂标签 + 扫码推广粘在正文之后
    ("“嗯，好吧，教授，我听懂了。”斯塔克点了点头。"
     "a>vas>div>扫码下载红袖联合潇湘送福利新人限时全场免费读div>div>div>",
     "斯塔克点了点头"),
    # 带域名点号的残留（在**对白里**）
    ("希望仙子姐姐不要介意啊。*一秒记住.xs222.*", "希望仙子姐姐不要介意啊"),
]

# **正文反例**：含同样的词，但都是正常叙述——一条都不许删
LIBRARY_PROSE = [
    "小樱声音轻轻的，微微点头后又翻开了下一页，下一页同样是一张插画，"
    "这一次是身穿体操服，手持体操棒的她。",
    "他点开订阅页面，把整本书都订阅了下来，然后继续往下读。",
    "“记得给我投推荐票。”少年咧嘴一笑，转身跑开了。",
    "少年正在手打铁，炉火映得他满脸通红。",
    "他必须一秒记住这个号码，否则明天就联系不上人了。",
    "他翻到书的最后一页，发现作者留了一行小字。",
]


def test_library_survey_junk_lines_removed():
    """真实书库里跨章重复出现的站点样板行必须被删掉。"""
    for line in LIBRARY_JUNK:
        out = clean_text("正文第一段。\n\n" + line + "\n\n正文第二段。")
        assert line not in out, f"这行站点样板没被清掉：{line[:40]!r}"
        assert "正文第一段。" in out and "正文第二段。" in out, "正文被误删"


def test_library_survey_inline_junk_removed_but_prose_kept():
    """行内垃圾粘在正文行尾时也要清掉，且**不能吃掉正文那半句**。"""
    for raw, must_keep in LIBRARY_INLINE:
        out = clean_text(raw)
        assert must_keep in out, f"正文被误删：{out[:60]!r}"
        assert "本站" not in out and "投推荐票" not in out, f"行内垃圾没清掉：{out[-60:]!r}"


def test_library_survey_prose_with_same_words_is_untouched():
    """含"下一页/订阅/投推荐票/手打"的**正常叙述**一句都不能碰。

    第一版判据容易在这里出事（历史上真发生过"对白密集被判段落空白异常"的误报），
    所以这些反例单独锁死。
    """
    for line in LIBRARY_PROSE:
        out = clean_text("正文第一段。\n\n" + line + "\n\n正文第二段。")
        assert line in out, f"正常句子被误删：{line[:40]!r}"
