"""手机导出残留的最小样本；不包含整本用户小说。"""

import pytest

from engine.cleaner import clean_text


@pytest.mark.parametrize("footer", [
    "《测试作品》爱下书小说网全文字更新,牢记网址:",
    "《测试作品》爱下书小说网全文字更新，牢记网址：https://example.com/",
])
def test_measured_site_footer_is_removed_without_touching_story(footer):
    story = "她合上书，走向窗边。\n远处的钟声响起。"
    assert clean_text(story + "\n" + footer) == story


@pytest.mark.parametrize("story", [
    "他说：‘《测试作品》爱下书小说网全文字更新,牢记网址:’，随后关掉网页。",
    "她在爱下书小说网找到一份资料。",
    "朋友向他推书《测试作品》，他认真记下书名。",
])
def test_story_mentions_are_not_classified_as_standalone_site_footer(story):
    assert clean_text(story) == story
