from engine.crawler import SourceCrawler


def get_book(html, rules=None):
    crawler = SourceCrawler({"bookSourceName": "隔离元数据", "bookSourceUrl": "https://metadata.invalid",
                             "ruleBookInfo": rules or {}})
    crawler.adapter = None
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        return html
    crawler.fetcher.get = fetch
    book = crawler.get_book("https://metadata.invalid/book/one")
    assert len(calls) == 1
    return book


def test_missing_rules_use_novel_metadata_before_generic_social_title():
    book = get_book('''<html><head>
        <meta content="社交站点标题" property="og:title">
        <meta CONTENT="  正确 &amp; 书名  " PROPERTY="OG:NOVEL:BOOK_NAME">
        <meta content="原作者" name="og:novel:author">
        </head></html>''')
    assert book.name == "正确 & 书名"
    assert book.author == "原作者"


def test_explicit_source_rules_remain_authoritative():
    book = get_book('''<h1>规则书名</h1><div class="author">规则作者</div>
        <meta property="og:title" content="其他标题"><meta name="author" content="其他作者">''',
        {"name": "//h1/text()", "author": "//div[@class='author']/text()"})
    assert book.name == "规则书名"
    assert book.author == "规则作者"


def test_generic_metadata_is_used_without_body_or_title_guessing():
    book = get_book('<meta content=" 通用\n书名 " property="og:title"><meta name="author" content="作者">')
    assert book.name == "通用 书名" and book.author == "作者"
    missing = get_book('<title>站点首页</title><h1>第一章</h1><p>《剧情提及的书》</p>')
    assert missing.name == "https://metadata.invalid/book/one"
    assert missing.author == ""


def test_empty_page_keeps_existing_url_fallback_without_exception():
    assert get_book("").name == "https://metadata.invalid/book/one"
