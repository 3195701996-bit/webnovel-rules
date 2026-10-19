"""保守的章节级清洗审查。只提供证据，不删除章节或修改缓存。"""

import re
import unicodedata


_NUMBERED = re.compile(r"^第[零〇一二三四五六七八九十百千万两\d]+[章卷节回篇]")
_PROMO_TITLE = re.compile(
    r"^(?:推[书書]《[^》\n]{1,100}》[!！。]?"
    r"|新[书書]《[^》\n]{1,100}》[^\n]{0,60}(?:追读|追讀|收藏|支持)[!！。～~]*"
    r"|[^\n]{0,35}(?:推波|推荐|推薦)朋友的[书書][^\n]{0,45})$"
)
_BOOK_NAME = re.compile(r"《([^》\n]{1,100})》")
_APPEAL = re.compile(r"(?:帮帮忙|幫幫忙|求.{0,5}(?:追读|追讀|收藏)|给他.{0,8}追读|給他.{0,8}追讀)")


def review_chapter(name, body):
    """高特异标题+正文佐证才建议审查；编号章节/普通引用不猜测。

    返回None或可序列化建议，绝不把建议直接当作删除授权。
    NFKC仅用于判别，不改用户原文和章节名称。
    """
    title = unicodedata.normalize("NFKC", str(name or "")).strip()
    if not title or len(title) > 200 or "\n" in title or _NUMBERED.match(title):
        return None
    if not _PROMO_TITLE.fullmatch(title):
        return None
    text = unicodedata.normalize("NFKC", str(body or ""))
    # 去掉可能重复写在缓存首行的标题，它自身不能充当第二份证据。
    lines = text.splitlines()
    start = 0
    while start < len(lines) and (not lines[start].strip() or lines[start].strip() == title):
        start += 1
    text = "\n".join(lines[start:])
    names = _BOOK_NAME.findall(title)
    if not (any(f"《{book}》" in text for book in names) or _APPEAL.search(text)):
        return None
    return {
        "kind": "promotion",
        "reason": "推广标题且正文含对应书名或追读诉求，请核对是否保留",
        "review_only": True,
    }
