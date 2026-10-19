"""不改缓存的阅读版TXT：连续标准标题、显式推广排除、内容级预览版本。"""

import hashlib
import json
import os
import re
import tempfile

from .app_utils import cache_key_of, text_write_lock
from .cleaner import clean_text
from .novel_review import review_chapter

_CHAPTER_PREFIX = re.compile(r'^第[零〇一二三四五六七八九十百千万两\d]+章[\s:：、.．]*')


def build_reader_export(book_dir, chapters, *, clean=True, omit_promotions=False):
    """返回匿名临时文件和预览清单；调用者负责关闭临时文件。

    每个导出章节按连续序号命名，原始序号/标题在清单中保留。
    不以相似正文、相同章号去重，不写book.txt、缓存、目录或阅读进度。
    """
    output = tempfile.TemporaryFile(mode='w+b')
    digest = hashlib.sha256(json.dumps({'format': 1, 'clean': clean, 'omit_promotions': omit_promotions}, sort_keys=True).encode())
    exported, omitted, missing = [], [], []
    try:
        for index, chapter in enumerate(chapters or [], 1):
            if not isinstance(chapter, dict) or not chapter.get('url'):
                raise ValueError('目录存在无效章节，无法生成阅读版')
            name = str(chapter.get('name') or '').strip()
            url = str(chapter['url'])
            path = os.path.join(book_dir, cache_key_of(url) + '.cache')
            if os.path.dirname(os.path.realpath(path)) != os.path.realpath(book_dir):
                raise ValueError('章节缓存路径异常')
            try:
                with text_write_lock(path), open(path, encoding='utf-8', newline='') as f:
                    raw = f.read()
            except FileNotFoundError:
                raw = None
            # 包括缺失位置和完整内容哈希，不只用mtime/size判断预览是否变化。
            digest.update(json.dumps([index, url, name, None if raw is None else hashlib.sha256(raw.encode()).hexdigest()], ensure_ascii=False).encode())
            if raw is None or not raw.strip():
                missing.append({'source_index': index, 'name': name})
                continue
            review = review_chapter(name, raw)
            if omit_promotions and review:
                digest.update(json.dumps(['omitted', review], ensure_ascii=False, sort_keys=True).encode())
                omitted.append({'source_index': index, 'name': name, 'reason': review['reason']})
                continue
            body = raw.strip()
            # 只移除完整相等的缓存首部标题，不用前缀或局部指纹猜测正文。
            if name:
                while body == name or body.startswith(name + '\n') or body.startswith(name + '\r\n'):
                    body = body[len(name):].lstrip('\r\n')
            if clean:
                body = clean_text(body)
            if not body.strip():
                raise ValueError(f'目录第{index}项处理后没有正文，请检查缓存或导出选项')
            title = re.sub(r'\s+', ' ', name).strip()
            title = _CHAPTER_PREFIX.sub('', title).strip()
            number = len(exported) + 1
            heading = f'第{number}章' + (' ' + title if title else '')
            text = body.replace('\r\n', '\n').replace('\r', '\n').strip().replace('\n', '\r\n')
            encoded = (heading + '\r\n\r\n' + text + '\r\n\r\n').encode('utf-8')
            digest.update(encoded)
            output.write(encoded)
            exported.append({'source_index': index, 'export_index': number, 'name': name, 'heading': heading})
        if not exported:
            raise ValueError('没有可导出的本地正文章节')
        output.seek(0)
        return output, {'revision': digest.hexdigest(), 'exported_count': len(exported),
                        'omitted_count': len(omitted), 'missing_count': len(missing),
                        'chapters': exported, 'omitted': omitted, 'missing': missing,
                        'clean': clean, 'omit_promotions': omit_promotions}
    except BaseException:
        output.close()
        raise
