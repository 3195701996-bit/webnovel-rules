import pytest

from tools.check_documentation_links import check_links, targets


@pytest.mark.parametrize("markup", [
    "[guide](guide.md)", "![demo](guide.md)", "[guide](<guide.md>)",
    '[guide](guide.md "title")', '<img src="guide.md" width="260">',
    "[guide](guide.md#section)",
])
def test_local_link_forms(tmp_path, markup):
    (tmp_path / "README.md").write_text(markup)
    (tmp_path / "guide.md").write_text("# Guide")
    assert check_links(tmp_path, ["README.md"]) == (1, [])


def test_missing_target_fails(tmp_path):
    (tmp_path / "README.md").write_text("[missing](missing.md)")
    checked, errors = check_links(tmp_path, ["README.md"])
    assert checked == 1 and "missing local target" in errors[0]


def test_external_and_fragment_links_are_not_network_requests(tmp_path):
    (tmp_path / "README.md").write_text("[a](https://example.invalid/) [b](#local) [c](//example.invalid/)")
    assert check_links(tmp_path, ["README.md"]) == (0, [])


def test_code_examples_are_not_links():
    assert targets("```md\n[a](missing.md)\n```\n`[b](missing.md)`") == []


def test_encoded_unicode_path(tmp_path):
    (tmp_path / "README.md").write_text("[guide](%E4%B8%AD%E6%96%87%20%E6%96%87%E6%A1%A3.md)")
    (tmp_path / "中文 文档.md").write_text("guide")
    assert check_links(tmp_path, ["README.md"]) == (1, [])


def test_link_escape_fails_even_if_file_exists(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "private.md").write_text("private")
    (root / "README.md").write_text("[outside](../private.md)")
    assert "escapes repository" in check_links(root, ["README.md"])[1][0]


def test_missing_document_fails(tmp_path):
    assert "Missing or out-of-root document" in check_links(tmp_path, ["missing.md"])[1][0]


def test_link_to_directory_is_allowed(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("[docs](docs/)")
    assert check_links(tmp_path, ["README.md"]) == (1, [])
