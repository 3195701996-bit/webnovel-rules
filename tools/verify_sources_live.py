#!/usr/bin/env python3
"""全量书源健康验证：搜索'剑来'/'斗破苍穹'，能返回真实书链接的保留，否则禁用。

标准：
- 搜索任一热门词，返回结果中含真实书 URL（/book//stone//read//N.html 等）→ valid
- 无搜索能力/返回影视分类页 → 禁用（enabled=false, valid=false, reason）
"""
import os
import re
import sys
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.crawler import SourceCrawler
from engine.source_mgr import _atomic_write, load_all

BOOK_RE = re.compile(r'/(book|stone|novel|read|html|info|books|b|txtxs|kan|new|article|xiaoshuo)/\d+|/\d+\.html')
BAD_RE = re.compile(r'vod|sort\d|/list/|/class/|/type/', re.I)
KEYWORDS = ['剑来', '斗破苍穹', '赘婿']


def check(src):
    uid = src.get('uid', '')
    name = src.get('bookSourceName', '')
    url = src.get('bookSourceUrl', '')
    try:
        c = SourceCrawler(src)
        for kw in KEYWORDS:
            books = c.search(kw)
            real = [b for b in books if BOOK_RE.search(b.get('book_url', ''))]
            real = [b for b in real if not BAD_RE.search(b.get('book_url', ''))]
            if real:
                return {'uid': uid, 'name': name, 'url': url, 'ok': True,
                        'kw': kw, 'book_url': real[0]['book_url'][:50],
                        'n': len(real)}
        # 所有关键词都无真书
        return {'uid': uid, 'name': name, 'url': url, 'ok': False,
                'reason': '搜索无真实书链接'}
    except Exception as e:
        return {'uid': uid, 'name': name, 'url': url, 'ok': False,
                'reason': f"{type(e).__name__}: {str(e)[:40]}"}


def main():
    from _probe_guard import ensure_isolated  # noqa: E402
    ensure_isolated()
    srcs = [s for s in load_all() if s.get('enabled', True)]
    print(f"验证 {len(srcs)} 个启用源...", flush=True)
    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(check, s) for s in srcs]
        for i, fut in enumerate(as_completed(futs), 1):
            results.append(fut.result())
            if i % 30 == 0:
                ok = sum(1 for r in results if r['ok'])
                print(f"[{time.strftime('%H:%M:%S')}] {i}/{len(srcs)} 有效={ok} 耗时{(time.time()-t0)/60:.1f}m", flush=True)
    json.dump(results, open('/tmp/sources_live_check.json', 'w', encoding='utf-8'),
              ensure_ascii=False)
    ok = [r for r in results if r['ok']]
    bad = [r for r in results if not r['ok']]
    print(f"完成: {len(results)} 源, 有效 {len(ok)}, 无效 {len(bad)}", flush=True)
    # 禁用无效源
    disabled = 0
    for r in bad:
        uid = r['uid']
        for src in srcs:
            if src.get('uid') == uid:
                src['enabled'] = False
                src['valid'] = False
                src['valid_error'] = r.get('reason', '')
                src['invalid_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
                p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 'sources', f"{uid}.json")
                _atomic_write(p, src)
                disabled += 1
                break
    print(f"已禁用 {disabled} 个无效源", flush=True)


if __name__ == '__main__':
    main()
