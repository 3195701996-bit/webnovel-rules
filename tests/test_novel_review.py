import pytest

from engine.novel_review import review_chapter


@pytest.mark.parametrize("title,body", [
    ("推书《测试作品》", "《测试作品》\n喜欢这类作品的可以看看。"),
    ("新书《测试作品》求追读！", "新书已开。求追读呀！"),
    ("6万字加更完成，顺便推波朋友的书，求帮忙追读～", "帮帮忙吧，就差两百个追读！"),
])
def test_promotion_requires_two_sources_of_evidence(title, body):
    result = review_chapter(title, body)
    assert result["kind"] == "promotion" and result["review_only"] is True


@pytest.mark.parametrize("title,body", [
    ("第101章 推书《测试作品》", "《测试作品》是剧情中的一本书。"),
    ("番外 一封信", "朋友请我推书《测试作品》。"),
    ("后记", "求追读，谢谢大家。"),
    ("推书《测试作品》", "推书《测试作品》\n\n他合上书，关掉了灯。"),
    ("推书《测试作品》", ""),
    ("他开始推书《测试作品》", "《测试作品》是他的最爱。"),
    ("新书《测试作品》出现了", "《测试作品》是剧情中的一本书。"),
    ("", "求追读！"),
])
def test_story_and_insufficient_evidence_are_not_promotions(title, body):
    assert review_chapter(title, body) is None
