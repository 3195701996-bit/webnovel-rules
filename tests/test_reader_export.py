import re

import pytest

from engine.app_utils import cache_key_of
from engine.reader_export import build_reader_export


def make_book(tmp_path):
    chapters = []
    for index, name in enumerate(['第一章 起点', '推书《测试作品》', '第一章 第二卷起点', '番外 一封信']):
        url = f'https://selftest.invalid/{index}'
        chapters.append({'name': name, 'url': url})
        body = '《测试作品》\n求帮忙追读！' if index == 1 else f'独特剧情{index}。\n小书亭app\n她关上门。'
        (tmp_path / (cache_key_of(url) + '.cache')).write_text(name + '\n\n' + body, encoding='utf-8')
    return chapters


def test_reader_edition_continuous_headings_and_opt_in_omission_are_non_destructive(tmp_path):
    chapters = make_book(tmp_path)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    artifact, report = build_reader_export(str(tmp_path), chapters, omit_promotions=True)
    with artifact:
        raw = artifact.read()
    text = raw.decode()
    assert re.findall(r'^第\d+章[^\r\n]*', text, re.M) == ['第1章 起点', '第2章 第二卷起点', '第3章 番外 一封信']
    assert '小书亭app' not in text and '独特剧情0' in text and '独特剧情2' in text
    assert report['omitted_count'] == 1 and report['omitted'][0]['source_index'] == 2
    assert report['chapters'][1]['source_index'] == 3 and report['chapters'][1]['export_index'] == 2
    assert b'\n' not in raw.replace(b'\r\n', b'')
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()}


def test_default_does_not_exclude_promotions_and_preserves_cache(tmp_path):
    chapters = make_book(tmp_path)
    artifact, report = build_reader_export(str(tmp_path), chapters, clean=False)
    with artifact:
        text = artifact.read().decode()
    assert report['exported_count'] == 4 and report['omitted_count'] == 0
    assert '推书《测试作品》' in text and '小书亭app' in text


def test_revision_tracks_content_even_with_same_file_size_and_options(tmp_path):
    chapters = make_book(tmp_path)
    artifact, before = build_reader_export(str(tmp_path), chapters)
    artifact.close()
    path = tmp_path / (cache_key_of(chapters[0]['url']) + '.cache')
    path.write_text(path.read_text().replace('剧情', '故事'), encoding='utf-8')
    artifact, after = build_reader_export(str(tmp_path), chapters)
    artifact.close()
    assert before['revision'] != after['revision']
    artifact, changed_option = build_reader_export(str(tmp_path), chapters, clean=False)
    artifact.close()
    assert after['revision'] != changed_option['revision']


def test_missing_cache_is_explicit_and_all_missing_is_error(tmp_path):
    chapters = [{'name': '第一章', 'url': 'https://selftest.invalid/missing'}]
    with pytest.raises(ValueError, match='没有可导出'):
        build_reader_export(str(tmp_path), chapters)


def test_revision_tracks_actual_cleaner_output_not_only_raw_cache(tmp_path, monkeypatch):
    chapters = make_book(tmp_path)
    artifact, before = build_reader_export(str(tmp_path), chapters)
    artifact.close()
    monkeypatch.setattr('engine.reader_export.clean_text', lambda text: '规则升级后的正文')
    artifact, after = build_reader_export(str(tmp_path), chapters)
    artifact.close()
    assert before['revision'] != after['revision']
