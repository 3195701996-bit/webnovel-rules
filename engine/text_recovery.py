"""清洗原文存档与条件恢复。存档成功之前绝不覆盖原缓存。"""

import hashlib
import json
import os
import re

from .app_utils import atomic_write, atomic_write_text, text_write_lock


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _cache_path(book_dir, filename):
    if (not isinstance(filename, str) or os.path.basename(filename) != filename
            or not filename.endswith('.cache') or filename in ('.cache', '..cache')):
        raise ValueError('非法章节缓存名称')
    path = os.path.join(book_dir, filename)
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(book_dir):
        raise ValueError('章节缓存不能指向书籍目录之外')
    return path


def _archive_dir(book_dir):
    path = os.path.join(book_dir, '.reclean-originals')
    if os.path.islink(path):
        raise ValueError('原文存档目录不能是符号链接')
    return path


def save_cleaned_cache(book_dir, filename, expected, cleaned):
    """精确比较旧文本；有并发下载变化则拒绝，不覆盖新内容。"""
    path = _cache_path(book_dir, filename)
    with text_write_lock(path):
        with open(path, encoding='utf-8', newline='') as f:
            current = f.read()
        if current != expected:
            raise ValueError('章节内容已变化，请重新预览后清洗')
        record = {'version': 1, 'filename': filename, 'original': current,
                  'original_sha256': _sha(current), 'cleaned_sha256': _sha(cleaned)}
        identity = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        archive = os.path.join(_archive_dir(book_dir), identity + '.json')
        # 内容寻址；重试同一转换不会不停产生备份。原文存档先fsync+replace。
        atomic_write(archive, record)
        atomic_write_text(path, cleaned)
        return identity


def restore_cleaned_cache(book_dir, identity, dry_run=True):
    """只恢复仍等于该次清洗结果的缓存；后续重爬/下载内容不得覆盖。"""
    if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{64}', identity):
        raise ValueError('非法原文存档编号')
    archive = os.path.join(_archive_dir(book_dir), identity + '.json')
    if os.path.islink(archive):
        raise ValueError('原文存档不能是符号链接')
    with open(archive, encoding='utf-8') as f:
        record = json.load(f)
    if (not isinstance(record, dict) or record.get('version') != 1
            or not isinstance(record.get('original'), str)
            or _sha(record['original']) != record.get('original_sha256')):
        raise ValueError('原文存档损坏，拒绝恢复')
    actual_id = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if actual_id != identity:
        raise ValueError('原文存档身份不匹配，拒绝恢复')
    path = _cache_path(book_dir, record.get('filename'))
    with text_write_lock(path):
        with open(path, encoding='utf-8', newline='') as f:
            current = f.read()
        if _sha(current) == record['original_sha256']:
            return {'restored': False, 'already_original': True, 'filename': record['filename']}
        if _sha(current) != record.get('cleaned_sha256'):
            raise ValueError('章节已有后续修改，拒绝覆盖，请人工核对')
        if not dry_run:
            atomic_write_text(path, record['original'])
        return {'restored': not dry_run, 'already_original': False, 'filename': record['filename']}
