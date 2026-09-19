#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本轮结构性优化的真实运行验证（2026-09-10）

在**真实 HTTP 服务 + 隔离数据目录**上验证三项结构性改动（pytest 之外
的最后一道防线，与 tools/smoke_api.py 同款起服方式）：

1. 导出版本机制：写入章节缓存 → GET txt 生成全文与 _export.json；
   修改缓存 → 再次 GET 自动重建（内容含新正文）；缓存不变 → 不重写
   （mtime_ns 相同，走新鲜路径）。
2. 中文 book_key 章节端点（R78 回归）：含中文目录名的 /chapter/<idx>
   必须 200 且带 ETag；此前 ETag 含中文触发 HTTP 头 latin-1 编码异常，
   服务端直接断连（导出正常、阅读不可用）。
3. 覆盖修复事务恢复：构造"崩溃在两次 rename 之间"的现场
    （staging.old 备份 + txn=backed_up + 目标缺失），在子进程启动前落盘。
    子进程导入 app 自动恢复后目标目录必须回滚恢复、记录清除。

运行：venv/bin/python tools/verify_runtime_round.py
退出码：0 = 全部通过；1 = 有失败项。
"""
import json
from pathlib import Path
import sys
import tempfile
import urllib.parse

from runtime_server import LocalServer

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def prepare_crash(data_dir):
    """Prepare only local fixture files, before the child imports app."""
    staging = data_dir / 'manga/_state/_repair_staging/copymanga_web/cidv/chv'
    backup = Path(str(staging) + '.old')
    target = data_dir / 'manga/downloads/copymanga_web/cidv/chv'
    backup.mkdir(parents=True)
    image = b'RIFF' + bytes(4) + b'WEBP' + bytes(64)
    for i in range(3):
        (backup / f'{i:04d}.webp').write_bytes(image)
    txn = Path(str(staging) + '.txn.json')
    txn.write_text(json.dumps({'staging': str(staging), 'backup': str(backup),
                              'target': str(target), 'state': 'backed_up',
                              'total': 3}), encoding='utf-8')
    return target, txn, backup, image


def main():
    # No app/config import in the parent: those modules bind data paths at import.
    from engine.app_utils import cache_key_of
    with tempfile.TemporaryDirectory(prefix='wr_verify_') as tmp:
        data_dir = Path(tmp) / 'data'
        src_dir = Path(tmp) / 'sources'
        src_dir.mkdir()
        target, txn, backup, image = prepare_crash(data_dir)
        bdir = data_dir / 'books/验证源_verify.test_cn1'
        bdir.mkdir(parents=True)
        chapters = [{'name': f'第{i}章', 'url': f'https://v.test/c{i}.html'}
                    for i in (1, 2)]
        for c in chapters:
            (bdir / (cache_key_of(c['url']) + '.cache')).write_text(
                f"{c['name']}\n\n{c['name']}正文" * 20, encoding='utf-8')
        (bdir / '_state.json').write_text(json.dumps({
            'book': {'name': '运行验证书', 'source_uid': 'deleted-source',
                     'book_url': 'https://v.test/book/1'},
            'chapters': chapters, 'completed': [c['url'] for c in chapters],
            'failed': {},
        }, ensure_ascii=False), encoding='utf-8')
        path = '/api/books/' + urllib.parse.quote(bdir.name)
        fails = []
        def check(ok, label):
            print(('PASS ' if ok else 'FAIL ') + label)
            if not ok:
                fails.append(label)
        with LocalServer(data_dir, src_dir) as server:
            check(target.is_dir() and not txn.exists() and not backup.exists()
                  and all((target / f'{i:04d}.webp').read_bytes() == image for i in range(3)),
                  '子进程启动自动恢复原章与清除事务记录')
            code, body, _ = server.request('GET', path + '/txt')
            check(code == 200 and '第1章' in body.decode() and '第2章' in body.decode(),
                  '书源已删除仍可首次离线导出')
            p = bdir / 'book.txt'
            if not p.exists():
                return 1
            mt = p.stat().st_mtime_ns
            meta = json.loads((bdir / '_export.json').read_text(encoding='utf-8'))
            check(bool(meta.get('rev')) and meta.get('txt_size') == p.stat().st_size,
                  '导出元数据与文件指纹一致')
            code, _, _ = server.request('GET', path + '/txt')
            check(code == 200 and p.stat().st_mtime_ns == mt, '新鲜导出不重写文件')
            c1 = bdir / (cache_key_of(chapters[0]['url']) + '.cache')
            c1.write_text('第1章\n\n重爬后的新正文' * 30, encoding='utf-8')
            code, body, _ = server.request('GET', path + '/txt')
            check(code == 200 and '重爬后的新正文' in body.decode(), '缓存变化自动重建')
            code, _, headers = server.request('GET', path + '/chapter/1')
            etag = headers.get('ETag', '')
            check(code == 200 and bool(etag) and etag.isascii(), '中文目录章节与 ASCII ETag')
            old = p.read_bytes()
            c1.write_bytes(b'\xff\xfeinvalid-cache')
            code, body, headers = server.request('GET', path + '/txt')
            check(code == 200 and headers.get('X-Export-Stale') == '1'
                  and body == old and p.read_bytes() == old, '损坏缓存保留旧导出且显式标记陈旧')
        check(server.process.poll() is not None, '仅本次临时服务退出并回收')
        print('RUNTIME: ' + ('ALL PASSED' if not fails else str(fails)))
        return int(bool(fails))


if __name__ == "__main__":
    sys.exit(main())
