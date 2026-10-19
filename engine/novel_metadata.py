"""User-confirmed display metadata lives separately from crawler-owned catalogs."""
import hashlib
import json
import os

from .app_utils import atomic_write, text_write_lock

FILENAME = '_metadata.json'


def overlay_metadata(state_path, state):
    if not isinstance(state, dict) or not isinstance(state.get('book'), dict):
        return state
    try:
        with open(os.path.join(os.path.dirname(state_path), FILENAME), encoding='utf-8') as stream:
            saved = json.load(stream)
        if saved.get('version') != 1 or saved.get('book_url') != state['book'].get('book_url'):
            return state
        fields = saved.get('metadata')
        if not isinstance(fields, dict):
            return state
        fields = {key: value for key, value in fields.items()
                  if key in ('name', 'author') and isinstance(value, str) and value.strip()}
        return dict(state, book=dict(state['book'], **fields))
    except (OSError, ValueError, TypeError, AttributeError):
        return state


def metadata_snapshot(state_path):
    with open(state_path, 'rb') as stream:
        raw = stream.read()
    state = json.loads(raw)
    if not isinstance(state, dict) or not isinstance(state.get('book'), dict):
        raise ValueError('书籍元数据格式异常')
    sidecar = os.path.join(os.path.dirname(state_path), FILENAME)
    try:
        with open(sidecar, 'rb') as stream:
            extra = stream.read()
    except FileNotFoundError:
        extra = b''
    if extra:
        saved = json.loads(extra)
        if (not isinstance(saved, dict) or saved.get('version') != 1
                or not isinstance(saved.get('metadata'), dict)
                or saved.get('book_url') != (state.get('book') or {}).get('book_url')):
            raise ValueError('已有元数据记录异常，已保留原件')
    revision = hashlib.sha256(raw + b'\x00metadata\x00' + extra).hexdigest()
    return overlay_metadata(state_path, state), revision


def save_metadata(state_path, revision, fields):
    if (not isinstance(fields, dict) or not fields or set(fields) - {'name', 'author'}
            or any(not isinstance(value, str) or not value.strip() or len(value) > 300
                   for value in fields.values())):
        raise ValueError('只允许非空书名和作者，每项最多300字符')
    fields = {key: ' '.join(value.split()) for key, value in fields.items()}
    if any(value.lower().startswith(('http://', 'https://')) for value in fields.values()):
        raise ValueError('网址不能作为修复后的书名或作者')
    with text_write_lock(state_path):
        state, current = metadata_snapshot(state_path)
        if revision != current:
            raise RuntimeError('本地信息已变化，请重新预览')
        info = state.get('book') if isinstance(state, dict) else None
        if not isinstance(info, dict) or not isinstance(info.get('book_url'), str):
            raise ValueError('书籍元数据格式异常')
        sidecar = os.path.join(os.path.dirname(state_path), FILENAME)
        if os.path.islink(sidecar):
            raise ValueError('元数据记录不能是符号链接')
        # Preserve previously confirmed fields, even if this preview changes only one.
        merged = {key: info.get(key, '') for key in ('name', 'author')}
        merged.update(fields)
        atomic_write(sidecar, {'version': 1, 'book_url': info['book_url'], 'metadata': merged})
        return merged
