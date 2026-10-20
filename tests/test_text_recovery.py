import json
import threading

import pytest

from engine import text_recovery as recovery
from engine.app_utils import atomic_write_text, text_write_lock


def test_cleaning_archive_roundtrip_and_dry_run_preserve_exact_bytes(tmp_path):
    path = tmp_path / '章节.cache'
    original = '标题\r\n\r\n原文。\r\n站点广告。'
    path.write_bytes(original.encode())
    identity = recovery.save_cleaned_cache(str(tmp_path), path.name, original, '标题\n原文。')
    assert recovery.restore_cleaned_cache(str(tmp_path), identity) == {
        'restored': False, 'already_original': False, 'filename': path.name}
    assert path.read_bytes() == '标题\n原文。'.encode()
    assert recovery.restore_cleaned_cache(str(tmp_path), identity, dry_run=False)['restored']
    assert path.read_bytes() == original.encode()
    assert recovery.restore_cleaned_cache(str(tmp_path), identity, dry_run=False)['already_original']


def test_archive_failure_never_overwrites_original(tmp_path, monkeypatch):
    path = tmp_path / 'chapter.cache'
    path.write_text('原文', encoding='utf-8')
    def fail(*args):
        raise OSError('no space')
    monkeypatch.setattr(recovery, 'atomic_write', fail)
    with pytest.raises(OSError):
        recovery.save_cleaned_cache(str(tmp_path), path.name, '原文', '清洗后')
    assert path.read_text() == '原文'


def test_new_download_content_blocks_stale_clean_and_restore(tmp_path):
    path = tmp_path / 'chapter.cache'
    path.write_text('原文', encoding='utf-8')
    identity = recovery.save_cleaned_cache(str(tmp_path), path.name, '原文', '清洗后')
    atomic_write_text(str(path), '新下载版本')
    with pytest.raises(ValueError, match='后续修改'):
        recovery.restore_cleaned_cache(str(tmp_path), identity, dry_run=False)
    with pytest.raises(ValueError, match='已变化'):
        recovery.save_cleaned_cache(str(tmp_path), path.name, '原文', '旧预览清洗结果')
    assert path.read_text() == '新下载版本'


def test_tampered_archive_and_path_traversal_are_rejected(tmp_path):
    path = tmp_path / 'chapter.cache'
    path.write_text('原文', encoding='utf-8')
    identity = recovery.save_cleaned_cache(str(tmp_path), path.name, '原文', '清洗后')
    archive = tmp_path / '.reclean-originals' / (identity + '.json')
    record = json.loads(archive.read_text())
    record['filename'] = '../outside.cache'
    archive.write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError):
        recovery.restore_cleaned_cache(str(tmp_path), identity, dry_run=False)
    with pytest.raises(ValueError):
        recovery.save_cleaned_cache(str(tmp_path), '../outside.cache', '', '')
    assert path.read_text() == '清洗后'


def test_atomic_writers_share_reentrant_path_lock(tmp_path):
    path = tmp_path / 'chapter.cache'
    entered = threading.Event()
    finished = threading.Event()
    def writer():
        entered.set()
        atomic_write_text(str(path), '下载内容')
        finished.set()
    with text_write_lock(str(path)):
        thread = threading.Thread(target=writer)
        thread.start()
        assert entered.wait(1)
        assert not finished.wait(.05)
        # 同线程重入不死锁。
        atomic_write_text(str(path), '清洗内容')
    thread.join(2)
    assert finished.is_set() and path.read_text() == '下载内容'


def test_archive_symlink_is_rejected(tmp_path):
    (tmp_path / 'elsewhere').mkdir()
    (tmp_path / '.reclean-originals').symlink_to(tmp_path / 'elsewhere', target_is_directory=True)
    path = tmp_path / 'chapter.cache'
    path.write_text('原文', encoding='utf-8')
    with pytest.raises(ValueError, match='符号链接'):
        recovery.save_cleaned_cache(str(tmp_path), path.name, '原文', '清洗后')
    assert path.read_text() == '原文'
