"""精华书阁章节名清洗：页面在章节名首尾插入混淆生僻字。
策略：GB2312 常用字判定 + 闭合标点兜底。"""
import re


def is_common(c):
    """判断是否为 GB2312 常用汉字"""
    try:
        c.encode("gb2312")
        return True
    except Exception:
        return False


def clean_chapter_name(name):
    """去掉精华书阁章节名的混淆噪声字"""
    if not name:
        return ""
    name = name.strip()
    m = re.search(r"(\d+、)", name)
    if m:
        body = name[m.end():]
        # 1) 截尾部连续2+非 GB2312 字（噪声）
        idx = len(body)
        run = 0
        for i, ch in enumerate(body):
            if ch.isdigit() or ch in "，。！？、（）()「」【】'\"《》—…·：":
                run = 0
                continue
            if is_common(ch):
                run = 0
            else:
                run += 1
                if run >= 2:
                    idx = i - run + 1
                    break
        tail = body[:idx].rstrip("，。！？、")
        # 2) 兜底：尾部含闭合标点（）】」等）时，其后内容全删
        m2 = re.search(r"^(.*?[）】」\"”!！?？。])", tail)
        if m2 and len(m2.group(1)) < len(tail):
            tail = m2.group(1)
        return m.group(1) + tail
    # 无序号（如"完结感言"）：去首尾非 GB2312 前缀/后缀
    i = 0
    while i < len(name) and not is_common(name[i]):
        i += 1
    name = name[i:]
    j = len(name)
    while j > 0 and not is_common(name[j - 1]):
        j -= 1
    return name[:j]
