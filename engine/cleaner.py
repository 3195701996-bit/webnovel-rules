#!/usr/bin/env python3
"""全方位文本净化模块（cleaner.py）

分层净化管线，每层处理一类污染，可独立测试：
  L1 基础清洗：HTML 标签、实体、空白、段首缩进
  L2 行内广告：嵌在正文中的网址/广告词（非整行）
  L3 整行污染：导航/书单/站点标识/纯短行
  L4 段落级：正文内嵌的广告段落、章节衔接残留
  L5 replaceRegex：书源自定义净化
"""
import re
import html as _html


# ── L2 行内广告模式 ────────────────────────────
# 嵌在正文中的网址/域名
INLINE_URL = re.compile(
    r'(?:https?://|www\.|[\w-]+\.(?:com|net|org|cc|info|xyz|top|la|tw|cn|me|io|vip|club|site|app|online|shop|tech|wang|ren|fun|live|pro|work|biz|space|link|city|pub|red|win|xin|group|cloud|store|world|asia|life))'
    r'[\w./:&?=%#~+@-]*'
)
# 站名+网址声明："天才一秒记住本站地址：[爱笔楼]http://..."
SITE_ADDR = re.compile(
    r'(?:天才一秒|一秒记住|请记住|记住本站|为了方便您|请您收藏|本站地址|永久地址|首发网址|最新网址|发布页|看书就上|手机版地址|电脑访问|本文地址|笔趣|新笔趣阁)[:：]?\s*'
    r'[\[【（(]?[^\]】）)）\s]{0,20}[\]】）)）]?\s*'
    r'(?:https?://|www\.)?[\w.-]+\.(?:com|net|org|cc|info|xyz|top|la|tw|cn|me|io|vip|club|site|app|online|shop|tech|wang|ren|fun|live|pro|work|biz|space|link|city|pub|red|win|xin|group|cloud|store|world|asia|life)[/\w./:&?=%#~+@-]*'
    r'\s*(?:最快更新[！!]?\s*无广告[！!]?|无广告[！!]?|请收藏|收藏本站|手机用户请浏览|防失联|最新章节|请牢记|最快更新)'
)
# R77（2026-09-18 逐源实测抓到的两类残留，都是"逐字可描述"的形态，用严格正则删）：
#   ① 换域名/最新地址广告**夹在正文段落中间**（没有换行）：
#      实测「最快更新请浏览器输入-JHSSD.COM-到新笔趣阁进行查看」
#   ② 导语式推广句（塞在章节开头一段）：
#      实测「一秒记住♂ ，更新快，，免费读！」
# 两条都要求同时出现"更新/记住"等站方口吻词与"输入/免费读"等动作词，
# 避免误伤正文里普通叙述（用例里有反例守护）。
SITE_REDIRECT_AD = re.compile(
    r'(?:最快)?更新(?:最快)?请?(?:浏览器)?输入[^\n]{0,60}?(?:查看|阅读|访问)'
    r'|请(?:浏览器)?(?:输入|访问)[^\n]{0,40}?(?:查看|阅读|访问)'
)
PROMO_LEAD_AD = re.compile(
    r'(?:天才)?一秒记住[^\n]{0,50}?(?:免费读|更新快|无弹窗|无广告)[^\n]{0,20}[！!。]?'
)

# 小说站通用广告语（行内）
INLINE_AD = re.compile(
    r'(?:天才一秒记住本站地址|一秒记住【.*?】|记住本站域名|支持正版|请到|手机用户请浏览|书友群|交流群|QQ群|V信|微信公众号|搜索一下|或者直接输入|小说app|app客户端|免费下载|下载txt|广告)'
)
# 中间弹窗残留："camelcotp();"、"function()"、"javascript:"
JS_RESIDUE = re.compile(r'camelcotp\s*\(\);?|javascript\s*:|function\s*\(\)\s*\{.*?\}|<script[\s\S]*?</script>|<!--[\s\S]*?-->')
# 章末/章首装饰线
# 0.60.0：补上实测出现的分隔符（※ ☆ ★ ◇ ◆ ■ □ ○ ● 等装饰符）——
# 纯符号行删掉；夹在正文行里的符号不动（避免误伤）。
DECOR_LINE = re.compile(
    r'^[-─━═—－_=~～*＊•·※☆★◇◆■□○●◤◢▽▼△▲]{2,}$'
    r'|^(?:[-─━═—－_=~～*＊•·※☆★◇◆■□○●◤◢▽▼△▲]\s*){3,}$')


# ── L3 整行导航词 ─────────────────────────────
NAV_WORDS = (
    '章节错误', '点此举报', '举报', '加入书签', '方便阅读', '上一页', '下一页', '上一章', '下一章', '下一节', '上一节',
    '返回目录', '回目录', '目录', '章节目录', '投票推荐', '推荐本书', '新书推荐',
    '本章未完', '请记住', '如果觉得', '请收藏', '手机阅读', '电脑阅读', '回到顶部',
    '点击下一页', '继续阅读', '最新章节', '本章完', '完本', '已更新', '字数',
    '本章节', '本节完', '正文卷', '第X卷', '更多章节', '看完整版', '阅读全文',
)
NAV_RE = re.compile(
    r'^[（(【\[『「]?(?:'
    + '|'.join(map(re.escape, NAV_WORDS))
    + r')'
    + r'(?:[，,、]?(?:' + '|'.join(map(re.escape, NAV_WORDS)) + r'))*'  # 多词组合
    + r'[）)】\]』」]?[\s:：.。！!？?，,]*$'
)

# 站点标识行
SITE_MARK_RE = re.compile(
    r'^(?:'
    r'(?=.*\d)(?=.*[a-zA-Z])[a-zA-Z0-9]{3,9}'                    # 7017k / 52xs8
    r'|\d{2,6}[a-zA-Z]?(?:小说网|中文网|书屋|书城|书库|阅读网|阅读|在线|免费|书院|文学|阁)'
    r'|(?:www\.)?[a-zA-Z0-9-]{2,20}\.(?:com|net|org|cc|info|xyz|top|la|tw|cn|me|io|vip|club|site|app|online|shop|tech|wang|ren|fun|live|pro|work|biz|space|link|city|pub|red|win|xin|group|cloud|store|world|asia|life)'
    r'|(?:最新|首发|官方|本站|本网站|本站域名|本站地址|永久)?(?:网址|地址|域名|发布页|书源|网站)[:：]?[\w./:_-]{1,30}(?:请收藏|请保存|防失联)?'
    r')$'
)

# 书单/推荐行（短行且含推荐特征）
RECOMMEND_RE = re.compile(
    r'^(?:推荐|推荐阅读|书单|好书推荐|其他作品|同类型|猜你喜欢|热门推荐|完本推荐|人气推荐|编辑推荐|最新书单|以下推荐)[:：]?\s*.{0,20}$'
)

# 章末作者的话/题外话
AUTHOR_NOTE_RE = re.compile(r'^[（(]?(?:作者|笔者|写手|码字)[:：]?的话?[）)]?[:：]?$|^(?:PS|ps|P.S)[:：]')

# 纯数字页
PAGE_NUM_RE = re.compile(r'^\d{1,4}$')

# L3 正文内章节标题残留（P2：原逐行 re.compile 提升为模块级，3000 行章省 3000 次编译）
L3_TITLE_RE = re.compile(
    r'^(第[一二三四五六七八九十百千万零两\d]+[章卷篇]|序章|楔子|番外|尾声|终章|后记|外传)[:：.。·\s]*')

# L6 营销广告模式（P2：原每次调用编译 2 个正则提升为模块级）
# 0.60.0：把实测到的**拼写变体**与**插入字**纳入（此前漏掉，用户导出的正文里成片残留）：
#   · 【新章节更新迟缓的问题，在能换源的app上终于有了解决之道，这里下载huanyuanap】
#     —— "换源"与"app"之间夹了"的"；下载域名写成 huanyuanap（只有一个 p）
# 做法：关键词之间允许 ≤3 个非换行字符（中英文夹字），并列出域名/变体拼写。
_APP_WORD = r'(?:[Aa][Pp]{2}|[Aa][Pp])'        # APP / App / app / ap
_CHANGE_SRC = (r'换\s*源(?:[^】\n]{0,3}?' + _APP_WORD + r'|[^】\n]{0,3}?(?:神器|阅读器)|)')
_ZHUI_SHU = (r'追\s*书(?:[^】\n]{0,3}?' + _APP_WORD + r'|[^】\n]{0,3}?(?:神器|阅读器)|)')
_HUANYUAN_HOST = r'huan\s*yuan\s*ap{1,2}'
L6_BLOCK_RE = re.compile(
    r'【[^】\n]{0,300}?(?:野果阅读|yeguoyuedu|' + _HUANYUAN_HOST + r'|'
    + _CHANGE_SRC + r'|' + _ZHUI_SHU + r'|'
    r'朗读听书|听书声音|离线朗读|老书友给我推荐|安卓苹果均可|'
    r'安装最新版|真特么好用|稳定运行多年|追更的好用|看书追更|'
    r'更新迟缓|解决之道)'
    r'[^】\n]{0,300}?】')
L6_STRONG_RE = re.compile(
    r'(?:yeguoyuedu|' + _HUANYUAN_HOST + r'|野果阅读|' + _CHANGE_SRC + r'|'
    + _ZHUI_SHU + r'|朗读听书|听书声音|小书亭|笔趣书阁|书荒网|'
    # 0.74.13 实测新增（思路客第131章章尾原样）：
    #   "本站重要通知:请使用本站的免费小说APP,无广告、破防盗版、"
    #   "看正版小说请到本站，无广告弹窗"
    # 只收**广告专属**措辞；"本站/APP"单独出现太宽（正文里也会提到），不收。
    r'本站重要通知|免费小说\s*[Aa][Pp]{1,2}|破防盗版|看正版小说)')

# 0.60.0：**整行就是站名/App 名**的广告（"小书亭app"、"笔趣书阁"…）。
# 严格限定：≤14 字、不含句末标点、且以 app/APP/阅读/神器/书阁/书屋 等结尾——
# 目的是删广告，**不是**删正文里提到 app 的句子（作者说"去下国家反诈APP！！！"必须保留）。
BARE_APP_LINE_RE = re.compile(
    r'^[\w\u4e00-\u9fff·]{1,12}(?:' + _APP_WORD + r'|阅读器?|神器|书阁|书屋|书城|小说网)$')

# 0.60.0：已知**逐字复现**的广告整句（精确匹配最安全：不动正文，只删这些句子）
KNOWN_AD_SENTENCES = (
    '新章节更新迟缓的问题，在能换源的app上终于有了解决之道，这里下载huanyuanap',
    '新章节更新迟缓的问题，在能换源的app上终于有了解决之道，这里下载huanyuanapp',
)

# ── 0.74.13 实测新增：适配器源正文里的残留（等长普查抓到，用户会读到）──────
# 取证（2026-09-18，`tools/probe_novel_content_parity.py` 跨源等长普查）：
#   · 思路客（isiluke）章尾：色情引流广告行 + "章节错误,点此报送(免注册), 报送后
#     维护人员会在两分钟内校正章节内容,请耐心等待。" + JS 残句 "read3();"
#   · 大帝书阁（ddsk）章尾：JSON 残块 `","message":"已经订阅 先定个小目标，比如
#     1秒记住：书客居手机版阅读网址：` 与 `('body')`
#   · 精华书阁（jhsssd）："阅读提示：为防止内容获取不全，请勿使用浏览器阅读模式。"
# 旧净化能删"天才一秒记住本站地址"这类老套路，但这几类**一条都没删掉**：
# 把上述四段喂给 clean_text，输出与输入等长（实测）。
# 判据只作用于**整行**——这些垃圾都是独立行，删整行不会碰到正文长句：
# 正文里提到"订阅/主播/注册"的句子不受影响（有测试锁这条边界）。
JUNK_LINE_RES = (
    # 站点"内容报错"提示行（各站文案不同，按实测形态拼）
    re.compile(r'^章节错误[,，]?点此(?:报[送错]|举报)[（(]?免?注册?[)）]?[,，]?'
               r'报送后维护人员会在两分钟内校正章节内容[,，]?请耐心等待[。.]?$'),
    re.compile(r'^阅读提示[:：]\s*为防止内容获取不全[,，]?\s*请勿使用浏览器阅读模式[。.]?$'),
    # JS 残句（read3(); / cambrian.render('body') / 光秃秃的 ('body')）
    re.compile(r'^(?:read\d*|cambrian\.render)\s*\(\s*[^)]{0,30}\)\s*;?$'),
    re.compile(r"^\(\s*'body'\s*\)$"),
    # 章尾轮换的引流广告。**必须带广告特征词组**，不能只凭"主播"两字——
    # 否则正文里"那位女主播走进了房间。"这种正常句子会被误删。
    re.compile(r'^.{0,60}(?:长按三秒复制|meinvmei\d{0,6}|'
               r'女主播.{0,20}(?:视频|关注|复制)|激[_\s]?情视频.{0,20}(?:关注|复制)).{0,60}$'),
    # JSON 残块与"记住本站"变体（ddsk 实测："先定个小目标，比如1秒记住：<站名>"）
    re.compile(r'^["\',:]*\s*"?message"?\s*[:：].{0,80}$'),
    re.compile(r'^.{0,40}先定个小目标[,，]?\s*比如1秒记住[:：].{0,40}$'),
    re.compile(r'^.{0,30}(?:书客居|手机阅读网址)[:：]?.{0,40}$'),
    # ── 2026-09-18 用户真实书库普查（6 本书 / 11695 章）补的一批 ─────────────
    # 方法：把"同一本书里跨章重复出现的短行"统计出来（正文不会重复，重复的
    # 基本都是站点样板），再逐条喂给 clean_text 确认"当前确实清不掉"。
    # 实测这批**一条都没被清掉**（输出里原样还在）。
    # 判据都带**站点自指**特征（书目/本站/投推荐票/手打/首发），
    # 不靠"下一页""订阅"这类正文里也会出现的词——实测反例
    # '翻开了下一页，下一页同样是一张插画' 必须保留（有测试锁这条）。
    re.compile(r'^小提示[:：].{0,80}(?:返回书目|返回上一[页章]).{0,80}$'),
    re.compile(r'^.{0,30}推荐一本好看的新作《.{1,20}》.{0,60}(?:收藏|订阅).{0,20}$'),
    re.compile(r'^.{0,10}一秒记住[，,]?\s*[為为]您提供精彩小说阅读[。.]?$'),
    re.compile(r'^\(\s*\.\s*\)\s*u$'),
    re.compile(r'^[ｈh]ttp\s*:?//\s*首发$'),
    re.compile(r'^正在手打中[,，].{0,60}(?:重新刷新页面|获取最新更新).{0,20}$'),
    re.compile(r'^首发最新[。.]?$'),
    re.compile(r'^[（(]\s*未完待续[。.]?\s*如果您喜欢这部作品.{0,80}[)）]$'),
    # 第二轮普查（同一批数据，清完上面那批后再扫出来的）：
    #   · '已加入书签' —— 站点 UI 提示被当正文存下来（实测独立成行）
    #   · '...手机用户请访问' —— 被截断的站点提示行
    #   · '1();您的支持，就是我们最大的动力。斗破苍穹，无弹窗，免费阅读，TXT…' —— JS 残句
    #     打头的整段推广（整行都是垃圾，按行删）
    re.compile(r'^已加入(?:书签|书架)[。.]?$'),
    re.compile(r'^\.{2,}\s*手机用户请访问.{0,60}$'),
    re.compile(r'^1\(\);\s*.{0,200}$'),
)

# 同一批实测垃圾的**行内**形态：它们常粘在正文行尾（不是独立行），
# 按整行判据删不掉。实测原样（思路客第131章行尾）：
#   '…、更新快,会员同步书架,请关注 gegegengxin (按住三秒复制) 下载免费阅读器!!
#     章节错误,点此报送(免注册), 报送后维护人员会在两分钟内校正章节内容,请耐心等待。'
# 注意"按住三秒复制"（不是"长按"）与"下载免费阅读器!!"——同一站的**轮换变体**，
# 所以这里按"固定句子/固定搭配"删，不做模糊匹配（避免误伤正文）。
INLINE_JUNK_RES = (
    # 注意 [，,]?\s* ：实测该句粘在正文行尾时逗号后带空格（"(免注册), 报送后…"）
    re.compile(r'章节错误[,，]?\s*点此(?:报[送错]|举报)[（(]?\s*免?注册?\s*[)）]?[,，]?\s*'
               r'报送后维护人员会在两分钟内校正章节内容[,，]?\s*请耐心等待[。.]?'),
    re.compile(r'请关注\s*\w{3,20}\s*[（(]\s*[按长]住三秒复制\s*[)）]\s*'
               r'下载免费阅读器[!！]*'),
    re.compile(r'[，,]?更新快[，,]?会员同步书架[，,]?'),
    re.compile(r'[（(]\s*[按长]住三秒复制\s*[)）]'),
    # ── 2026-09-18 真实书库普查补的**行内**形态（粘在正文行尾，整行判据删不掉）──
    # 实测原文：'…没有急于扑入负笈生怀中。shouda8本章节狂人手打一秒记住本站。'
    #                     '…向大家请假一晚！(未完待续。如果您喜欢这部作品，欢迎您来起点（）投推荐票、月票，您的支持，就是我最大的动力。)'
    # 两处都带站点自指（本站 / 未完待续+投推荐票），不靠通用词，避免误伤正文；
    # `(?:[A-Za-z]{3,20}\d{0,4})?` 是那截域名残渣（shouda8），只在紧跟"本章节…本站"时才算。
    re.compile(r'(?:[A-Za-z]{3,20}\d{0,4})?本章节(?:狂人手打)?(?:一秒记住|记住)本站(?:地址|域名)?[。.]?'),
    # 起点样板句：句中还嵌着一对 `起点（）`，所以**不能用非贪婪到第一个右括号**
    # （会把 `…欢迎您来起点（` 当结尾，留下半截"投推荐票、月票…"）。
    # 用模板自带的「您的支持」当锚点，右括号必须落在它之后。
    re.compile(r'[（(]\s*未完待续[。.]?\s*如果您喜欢这部作品.{0,120}?'
               r'(?:您的支持|就是我最大的动力).{0,30}?[)）]'),
    # 第二轮普查补的行内形态（原文照抄）：
    #   '…气象宏阔许多。一秒记住本站百度搜23文学网即可找到本站.fs23.{请在百度搜索1t;stronggt;1t;/stronggt;，全文字阅读}'
    #   '…那么我们从哪儿开始？”a>vas>div>扫码下载红袖联合潇湘送福利新人限时全场免费读div>div>div>'
    # 「记住本站」带站点自指；转义 HTML 残渣（`1t;stronggt;` 是 `&lt;strong&gt;` 被二次转义）
    # 与 `a>vas>div>` 这种碎裂标签在中文正文里不可能出现。
    re.compile(r'(?:一秒记住|请记住|记住)本站(?:百度搜.{0,60})?'),
    re.compile(r'请在百度搜索.{0,80}'),
    re.compile(r'1t;/?(?:strong|div|br|p|span)gt;'),
    re.compile(r'(?:[a-z]{1,5}>){3,}[^\n]*'),
    # 带域名点号的残留（原文照抄：'希望仙子姐姐不要介意啊。*一秒记住.xs222.*'）。
    # **必须带 `.<字母数字>`**才算：只写"一秒记住"会碰到正文里"一秒记住这个号码"这类
    # 正常句子（历史上就因为这类模糊匹配误删过正文）。
    re.compile(r'[*＊]?\s*一秒记住[.．][a-z0-9]{2,20}[.．]?\s*[*＊]?'),
)


# ── 主入口 ─────────────────────────────────────
def html_to_text(text):
    """HTML → 纯文本的公共实现（crawler.get_content / 适配器 _clean_content /
    cleaner L1 共用）：
    块级闭合标签与 <br> → 换行、去标签、实体反转、段首缩进与多余空白压缩。
    R47 收敛：此前同一管线在 crawler/adapters/cleaner 各抄一份。"""
    if not text:
        return ''
    text = re.sub(r'</(p|div|section|article|h\d|li)>', '\n', text, flags=re.I)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = _html.unescape(text)
    # 段首缩进（\xa0/全角空格）
    text = '\n'.join(l.strip() for l in text.split('\n'))
    text = re.sub(r'[ \t\xa0\u3000]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def clean_text(text, replace_regex=''):
    """全方位净化文本。replace_regex：书源自定义净化（##pat##repl 格式）"""
    if not text:
        return ''
    # L1 基础
    text = _L1_basic(text)
    # L8 整块重复去重（分页把同一页追加了两遍时，去掉后面那一份）。
    # **必须紧跟 L1**：分页重复是**逐行精确**的，而 L2/L4 会改动行内容，
    # 之后再找就找不到精确重复了（实测：放在末尾时漏掉一半）。
    text = _L8_dedup_blocks(text)
    # L2 行内广告
    text = _L2_inline(text)
    # L4 段落级（章节残留合并）
    text = _L4_paragraph(text)
    # L3 整行过滤（在 L2/L4 之后做，此时污染已暴露为独立行）
    text = _L3_lines(text)
    # L6 营销广告深度清除（野果阅读/换源App/追书神器等轮换文案）
    text = _L6_market(text)
    # L7 段落结构修复（源站把整章塞成一行时补换行；**不增删任何字符**）
    fixed = _L7_reparagraph(text)
    if fixed != text:
        text = fixed
        # 补完换行后再过一次整行过滤：原本"墙"里根本暴露不出来的整行垃圾，
        # 切出段落之后可能就成行了（这一遍是幂等的）
        text = _L3_lines(text)
    # L9 章尾残片（实测：抓取时把标签边界后的残余带进正文）
    text = _L9_strip_tail_residue(text)
    # L5 replaceRegex
    if replace_regex and '##' in replace_regex:
        text = _L5_replace(text, replace_regex)
    return text.strip()


# 整块去重判据：连续 ≥3 行、每行 ≥10 字、整块 ≥200 字，且**逐字节相同**
# （阈值由真实书库实测选定：三个变体都做到"删掉的每一行在原文都出现过 ≥2 次"0 处不安全，
#  这个最紧的一档能多修 1 章 —— 页缝重叠那种短块也能清掉。）
DUP_BLOCK_MIN_PARAS = 3
DUP_BLOCK_MIN_LEN = 10
DUP_BLOCK_MIN_CHARS = 200


def _L8_dedup_blocks(text, _pass=0):
    """同一章里**整块重复**时，去掉后面那一份。

    实测背景（2026-09-18，用户真实书库）：
      · yetianlian 第1384章 79 段里，**第 29~52 段与第 53~76 段完全相同**（重复 24 段）；
      · quben8 第1113章重复 15 段 —— 两者都**正好是一页正文的量**，
        即分页时把同一页追加了两遍（站点"下一页"指回了已取过的页 / 同一内容换了 URL）。
    全库统计：11506 章里 6 章如此（最多的一章有 13% 是重复内容）。

    判据保守到几乎不可能误伤原创内容：**连续 ≥3 段**、每段 **≥10 字**、
    整块 **≥200 字**，且段与段**逐字节相同**。短对白、口头禅和排比句够不上
    整块阈值；只删**后面**那一份，前面保留。
    """
    if _pass >= 4 or not text:
        return text
    lines = text.split("\n")
    idx = [i for i, l in enumerate(lines) if len(l.strip()) >= DUP_BLOCK_MIN_LEN]
    if len(idx) < DUP_BLOCK_MIN_PARAS * 2:
        return text
    for k in range(min(60, len(idx) // 2), DUP_BLOCK_MIN_PARAS - 1, -1):
        seen = {}
        for a in range(len(idx) - k + 1):
            key = tuple(lines[idx[a + j]].strip() for j in range(k))
            if key in seen:
                start, end = idx[a], idx[a + k - 1]
                span = sum(len(lines[x].strip()) for x in range(start, end + 1))
                if span < DUP_BLOCK_MIN_CHARS:
                    seen[key] = a          # 太短：可能是正文里的排比/口头禅，不算
                    continue
                return _L8_dedup_blocks("\n".join(lines[:start] + lines[end + 1:]),
                                        _pass + 1)
            seen[key] = a
    return text


# 句末标点（章尾残片只允许出现在这些标点**之后**）
_TAIL_ANCHOR = "。！？…”\"』」）)"
# 实测站名残片（全库 11695 章普查 Top 形态，2026-09-18）
_TAIL_JUNK_WORDS = ("本书来自", "无弹窗小说网", "小说网", "全文阅读", "最新章节",
                    "手机阅读", "免费阅读")
# ASCII 残片：1~8 个字母数字点横线（实测 `r1058`/`rt`/`v`/`u`/`bk`/`i`/`jpg`…）
_TAIL_ASCII = re.compile(r'[A-Za-z0-9._\-]{1,8}')


def _L9_strip_tail_residue(text):
    """清掉正文**末尾**粘着的站点残片（抓取时标签边界后的残余字符）。

    实测背景（2026-09-18，全库 11695 章普查）：
      · **935 章**章尾带残余，243 种形态；最典型的是 `…“是两指。”小说网`、
        `…算得了什么？.t`、`…面无表情。h`、`…双手合十。h`（爱下书 476 章、夜天连看 288 章）；
      · Top 形态：`r1058` 84×、`rt` 50×、`v`/`u` 各 24×、`bk` 23×、`i` 17×、
        `本书来自` 36×、`无弹窗小说网` 32×、`jpg` 8×。

    判据（**只动末尾、只动句末标点之后**，宁可不删也不误伤）：
      · 紧跟在**句末标点**（。！？…”』」）)）之后；
      · 内容是 1~8 个 ASCII 字母数字点横线，**或**在实测站名表里；
      · 剥掉后正文仍 ≥200 字（防止把整章当残片）。
    **不处理**"只剩标点"的残余（实测 `，` 9 章、`“` 6 章）：那是正文自己的结尾
    （多半是站点侧截断），删标点属于改正文，不在这里做。
    """
    if not text or len(text) < 200:
        return text
    stripped = text.rstrip()
    # **从右往左**找标点：取最靠后、且其后确实是残片的那个标点。
    # （第一版用 `re.search` 取到最靠左的标点 ✗ —— `…“是两指。”小说网` 里它在 `“` 上匹配，
    #   把正文也当成 frag，于是残片判不出来。）
    for i in reversed([m.start() for m in
                       re.finditer('[' + re.escape(_TAIL_ANCHOR) + ']', stripped)]):
        frag = stripped[i + 1:].strip()
        if not frag or len(frag) > 12:
            continue
        if _TAIL_ASCII.fullmatch(frag) or frag in _TAIL_JUNK_WORDS:
            cut = stripped[:i + 1].rstrip()
            return cut if len(cut) >= 200 else text
    return text



# 超长行阈值：单行 ≥ 这个字数就认为"段落结构丢了"（要切开）
WALL_LINE_MAX = 1500
# 补出来的段落目标长度（在句末标点处切，尽量贴近这个值）
WALL_TARGET = 300
# 句末标点（含成对收尾符号：切在这些符号**之后**，不把引号留在下一段开头）
_SENT_END = "。！？…"
_CLOSERS = "”\"』」）)】"


def _L7_reparagraph(text):
    """把**超长行**按句末标点切开，补回段落结构。

    实测背景（2026-09-18，拿用户真实书库当证据）：
      · 爱下书《雪中悍刀行》**186/977 章**的章节页里 `<br>` 数 = 0、换行数 = 0，
        整章 1.5 万字**就是一整行**（同一本书的正常章有 `<br>`=160）——读者看到的是一堵墙；
      · 全库 11695 章里"最长行"分布：**11107 章 < 400 字**（正常），
        400~1500 字 446 章，**≥1500 字只有 142 章**——阈值取 1500 分得很开。

    口径（外科式，只动超长的那一行）：
      · 逐行看，**只有 ≥ `WALL_LINE_MAX` 的行**才切；其余行与章一律不碰；
      · 只在**句末标点之后**切，且**一个字符都不增删**
        —— `"".join(切出的段)` 必须与原行完全相同（有测试 + 全库核对锁这条不变式）；
      · 段长目标 `WALL_TARGET`，超长行会被切成十几段，正常段落长度看着舒服。
    """
    if not text or "\n" not in text and len(text) < WALL_LINE_MAX:
        return text
    out, changed = [], False
    for line in text.split("\n"):
        if len(line) < WALL_LINE_MAX:
            out.append(line)
            continue
        parts, buf, i = [], "", 0
        while i < len(line):
            ch = line[i]
            buf += ch
            if ch in _SENT_END:
                # 收尾引号/括号并进本段，别让它们掉到下一段开头
                while i + 1 < len(line) and line[i + 1] in _CLOSERS:
                    i += 1
                    buf += line[i]
                if len(buf) >= WALL_TARGET:
                    parts.append(buf)
                    buf = ""
            i += 1
        if buf:
            parts.append(buf)
        if len(parts) < 2:
            out.append(line)          # 切不动（没有句末标点）→ 原样保留
            continue
        assert "".join(parts) == line, "补段落不得增删任何字符"   # 不变式，出错就地暴露
        out.extend(parts)
        changed = True
    # 保留原章节中每一条既有换行（包括空行）。只有墙体行内部新增段落分隔；
    # 不能因为同章存在一条墙体行，就把所有普通行重排成双换行。
    return "\n".join(out) if changed else text


def _L1_basic(text):
    """基础清洗：HTML→文本（含块级标签换行，与 crawler/适配器同一实现）、
    去 JS 残留块"""
    text = html_to_text(text)
    # 去除 JS 残留块
    text = JS_RESIDUE.sub('', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def _L2_inline(text):
    """行内广告清除（嵌在正文中，非整行）"""
    # 站名+网址声明（整段含网址的；网址后仅广告语或行尾，不吞标题）
    text = SITE_ADDR.sub('', text)
    # 兜底：站名词+网址段（只删到网址结束，不吞后续标题）
    text = re.sub(
        r'(?:最新网址|首发网址|永久地址|本站地址|看书就上|手机版地址|天才一秒记住本站地址|一秒记住)[:：]?\s*'
        r'[\[【（(]?[^\]】）)）\s]{0,15}[\]】）)）]?\s*'
        r'(?:https?://|www\.)?[\w.-]+\.(?:com|net|org|cc|info|xyz|top|la|tw|cn|me|io|vip|club|site|app|online|shop|tech|wang|ren|fun|live|pro|work|biz|space|link|city|pub|red|win|xin|group|cloud|store|world|asia|life)'
        r'/?[/\w./:&?=%#~+@-]*', '', text)
    # 行内域名（前后是标点/空格）
    text = re.sub(r'(?<![a-zA-Z0-9])' + INLINE_URL.pattern + r'(?![a-zA-Z0-9])', '', text)
    # 常见行内广告语
    text = re.sub(r'(?:天才一秒记住本站地址|一秒记住【[^】]*】|记住本站域名[^\s，。！？]*|'
                  r'请记住本站[^\s，。！？]*|手机用户请浏览[^\s，。！？]*|'
                  r'支持正版[^\s，。！？]*|书友群[^\s，。！？]*|交流群[^\s，。！？]*|'
                  r'QQ群[:：]?\d*|微信公众号[:：]?[^\s，。！？]*|'
                  r'下载txt[^\s，。！？]*|小说app[^\s，。！？]*|'
                  r'app客户端[^\s，。！？]*|免费下载[^\s，。！？]*)', '', text)
    # 站内推广引导："百度一下"XXX顶点小说"最新章节第一时间免费阅读。"
    text = re.sub(
        r'(?:百度一下|百度搜索|请百度|搜一下|搜索一下|请搜索|请百度搜索)'
        r'["“\'《]?[^"”\'》]{2,30}["”\'》]?'
        r'(?:最新章节?)?(?:第一时间)?免费阅读。?', '', text)
    # 变体：行内"书名+顶点小说+最新章节"推广（无百度前缀）
    text = re.sub(
        r'["“\'《][^"”\'》]{2,30}顶点小说["”\'》]最新章节第一时间免费阅读。?', '', text)
    # 章末推广："本章完。本书首发于XXX，请支持正版" 等
    text = re.sub(
        r'本章完[。！？]?[^。！？\n]{0,40}(?:首发|正版|支持|本站|请到)[^。！？\n]{0,30}[。！？]?', '', text)
    # 独立"本章完"导航行（L3 兜底此处行内）
    text = re.sub(r'[（(]?本章完[）)]?[。！？]?\s*$', '', text)
    # R77：换域名广告（夹在段落中间）+ 导语式推广句
    text = SITE_REDIRECT_AD.sub('', text)
    text = PROMO_LEAD_AD.sub('', text)
    # 装饰线（行内）
    text = DECOR_LINE.sub('', text)
    # 0.74.13 实测：行内形态的站点垃圾（固定句子/固定搭配，见 INLINE_JUNK_RES）
    for _rx in INLINE_JUNK_RES:
        text = _rx.sub('', text)
    return text

def _L3_lines(text):
    """整行过滤：导航/站点标识/书单/纯数字页"""
    keep = []
    for ln in text.split('\n'):
        sl = ln.strip()
        if not sl:
            keep.append('')
            continue
        compact = re.sub(r'\s', '', sl)
        # 导航词整行
        if NAV_RE.match(compact):
            continue
        # 站点标识
        if SITE_MARK_RE.match(compact):
            continue
        # 书单推荐
        if RECOMMEND_RE.match(compact):
            continue
        # 作者的话
        if AUTHOR_NOTE_RE.match(compact):
            continue
        # 纯数字页
        if PAGE_NUM_RE.match(compact):
            continue
        # 0.60.0：整行就是 App 名/站名（"小书亭app"）——严格模式，不碰正文句子
        if BARE_APP_LINE_RE.match(compact):
            continue
        # 孤立分页残留："页)" / "3页)" / "第1/3页)"（旧净化遗留）
        if re.fullmatch(r'(?:[\d第/]*页\)|[\d/]+页\))', compact):
            continue
        # 0.74.13 实测垃圾整行（报错提示/JS 残句/引流广告/JSON 残块）——
        # 判据只作用于整行，正文长句不受影响（JUNK_LINE_RES 处有取证说明）
        if any(rx.match(compact) for rx in JUNK_LINE_RES):
            continue
        # 面包屑/路径："首页 > 小说 > 书名 > 章节"
        if re.match(r'^[^\n]{0,20}(?:首页|小说|书城)[\s>»›]', sl) and '>' in sl:
            continue
        # 装饰线整行
        if DECOR_LINE.match(compact):
            continue
        # 站名+网址整行（残留未清）
        if re.match(r'^[^\n]{0,25}(?:网址|地址|域名|发布页|收藏|看书)[^\n]*[.:：]\s*\S', sl):
            continue
        # 章节标题分页标记："第三章 xxx (第1/3页)" → 去标记
        sl = re.sub(r'\s*[（(]?第\d+/\d+页[）)]?\s*$', '', sl)
        if not sl.strip():
            continue
        # 正文内章节标题残留：
        # 1) 整行标题（标题后无正文）→ 删除
        # 2) 行首"第X章 xxx"后紧跟正文（分页合并残留）→ 剥离标题部分
        _compact2 = re.sub(r'\s', '', sl)
        _tm = L3_TITLE_RE.match(_compact2)
        if _tm:
            _after = _compact2[_tm.end():]
            # 标题后是行尾 → 纯标题，删除
            if not _after:
                continue
            # 找标题名结束（第一个句号/冒号/感叹号/问号）
            _punct = re.search(r'[。！？!?：:]', _after)
            if _punct:
                # 标题名 + 标点 + 正文 → 保留正文部分
                _body = _after[_punct.end():]
                if _body:
                    sl = _body
                    keep.append(sl)
                    continue
                else:
                    continue  # 标点后无内容 → 纯标题
            # 无标点：纯标题行（独立章节标题），删除
            continue
        keep.append(sl)
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(keep))


def _L4_paragraph(text):
    """段落级：正文内嵌广告段、章节衔接残留"""
    # "（本章未完，请点击下一页继续阅读）" 等
    text = re.sub(r'[（(]?本章未完[^）)]*[）)]?', '', text)
    text = re.sub(r'[（(]?请(?:点击|继续|翻到)[^）)]*[）)]?', '', text)
    # "——分割线——" / "未完待续"
    text = re.sub(r'[-─━═]{2,}(?:分割线|分隔线)?[-─━═]{2,}', '\n', text)
    # 尾注残留："camelcotp();"（L2 已处理，此处兜底）
    return text


def _L5_replace(text, replace_regex):
    """书源 replaceRegex（##pat##repl 格式）"""
    from .rules import split_clean_suffix
    _, cleans = split_clean_suffix(replace_regex)
    for pat, repl, replace_first in cleans:
        try:
            _pat = pat.replace('\\\\', '\\')
            _repl = re.sub(r'\$(\d+)', r'\\\1', repl.replace('\\\\', '\\'))
            if replace_first:
                # 与 rules.RuleAnalyzer.get_string 的既有契约保持一致：
                # ##pat##repl## 取第一处匹配、对其做替换，返回加工后的匹配本身；
                # 未命中返回空串（上轮曾改为“替换全文第一处 count=1”，与字段取值
                # 侧的 extract-first 语义分叉；此处恢复一致，避免同一 ## 语法在
                # 字段取值与正文清洗两处产生漂移）。
                m = re.search(_pat, text)
                text = re.sub(_pat, _repl, m.group(0), count=1) if m else ''
            else:
                text = re.sub(_pat, _repl, text)
        except Exception:
            pass
    return text


# ── 便捷：清理单章（缓存级，与 get_content 兼容）──
def _L6_market(text):
    """营销广告深度清除：各小说站轮换的"野果阅读/换源App/追书神器"推广
    （多为【…】括号块或独立句插在正文段间）。逐句处理防误删正文长句：
    - 整块【…】含特征词 → 删块（先做，块可能嵌在句中）
    - 短句(≤160字)含强特征词 → 删整句
    - 长句仅靠块删除，不动正文
    （P2：_BLOCK/_STRONG 已提升为模块级 L6_BLOCK_RE / L6_STRONG_RE）
    """
    text = L6_BLOCK_RE.sub('', text)
    # 已知整句广告（含括号包裹的形态）：先按精确句子删，再走通用规则
    for _sent in KNOWN_AD_SENTENCES:
        text = text.replace('【' + _sent + '】', '')
        text = text.replace(_sent, '')
    # 逐句处理：切分保留句界符
    parts = re.split(r'([。！？!?])', text)
    out = []
    sent = ''
    for p in parts:
        sent += p
        if p and p in '。！？!?':
            if len(sent) <= 160 and L6_STRONG_RE.search(sent):
                sent = ''          # 广告句删除
            else:
                out.append(sent)
                sent = ''
    if sent:
        if len(sent) <= 160 and L6_STRONG_RE.search(sent):
            pass
        else:
            out.append(sent)
    text = ''.join(out)
    text = re.sub(r'[ \t\xa0\u3000]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


# ── 净化质量自检（0.74.6）────────────────────────────────────────────────
# 为什么放在引擎里而不是探针脚本里：App 的「书源验证」也要能记录"正文干不干净"，
# 两侧共用一份判据（各写一份必然漂移）。只做**能自动判定**的几项：
# 广告/导航残留、过短、段落空白异常。"是不是真正文"仍需人工看开头片段
# （源站常把作者感言当第 1 章，那不是缺陷）。
AD_RESIDUE_MARKERS = (
    "下一页", "最新网址", "请记住本站", "记住本站", "手机用户请", "天才一秒",
    "一秒记住", "笔趣", "全本小说", "app下载", "APP下载", "扫码", "关注公众号",
    "本章未完", "点击下一页", "免费阅读", "加入书签", "投推荐票", "换源",
    # 0.74.13 实测补充：净化器现在会删掉它们，所以它们**出现在文本里**就是异常
    # （逐源实测抓到过：思路客章尾色情引流、精华书阁"阅读提示"、大帝书阁 JSON 残块）
    "长按三秒复制", "请勿使用浏览器阅读模式", "已经订阅", "先定个小目标",
    "报送后维护人员", "cambrian.render",
)


def check_purify_quality(text):
    """净化后正文的质量自检 → (ok, [问题描述...])。纯函数、不联网、不写盘。"""
    probs = []
    if not text:
        return False, ["正文为空"]
    n = len(text)
    if n < 300:
        probs.append("过短(%d 字)" % n)
    hits = [m for m in AD_RESIDUE_MARKERS if m in text]
    if hits:
        probs.append("疑似广告/导航残留：" + "、".join(hits[:4]))
    # 空行广告位判据（0.74.17 修误报）：旧判据是"\n\n 次数 > max(20, 字数//40)"，
    # 等价于"平均段长 < 40 字"，而**中文小说对白密集**时平均段长本就 26~37 字——
    # 实测（2026-09-18）精华书阁《剑来》第522章：14912 字/395 个 "\n\n"（阈值 372）
    # 被判"段落空白异常"，但**连续空行 0 处**、段落中位 29 字、全是正常对白；
    # 啃书网第278章同理（189 vs 176）。这条判据会被逐源实测写进"净化质量"给用户看，
    # 误报等于无端给好源扣帽子。
    # 真正的空行广告位残留是"删掉广告行后留下的连续空行"（\n\n\n）——
    # 实测干净正文为 0 处，故按**连续空行组**判，且给足余量。
    _blank_runs = len(re.findall(r"\n[ \t]*\n[ \t]*\n", text))
    if _blank_runs > max(3, n // 2000):
        probs.append(f"疑似空行广告位残留（连续空行 {_blank_runs} 处）")
    return (not probs), probs


def clean_cache_text(text):
    """清洗已有缓存文本（含标题行保护：首行是章节名）"""
    lines = text.split('\n', 1)
    title = lines[0] if lines else ''
    body = lines[1] if len(lines) > 1 else ''
    cleaned = clean_text(body)
    if title.strip():
        return f"{title.strip()}\n\n{cleaned}"
    return cleaned
