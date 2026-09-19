# 专属书源适配器（engine/adapters/）

> 背景：legado 套用规则引擎对部分站点失效（JS 渲染目录/正文、站点改版、
> 章节名混淆、正文粘段等）。为此引入"每源一适配器"架构：为每个网站编写
> 独立的 Python 爬虫适配器，硬编码该站点的真实结构，替换通用规则引擎。

## 架构

```
engine/adapters/
├── __init__.py        # 注册表 _REGISTRY + resolve_adapter() 分发
├── _jhsssd_clean.py   # 精华书阁章节名噪声清洗（GB2312常用字判定）
├── tadu.py            # 塔读：正文 hidden input API + 目录CSS + 水印清除
├── jhsssd.py          # 精华书阁：目录分页 + 章节名清洗 + 正文分页合并
├── biquge_common.py   # 笔趣阁经典模板（18源共用）：search.php + dd a + readcontent
└── sfacg.py           # SF轻小说（已删付费源，备用）
```

### 工作方式
1. `SourceCrawler.__init__` 调用 `resolve_adapter(source, fetcher)`
2. 按 `bookSourceUrl`/`uid` 匹配注册表 → 返回适配器实例
3. 命中适配器后：`search/get_book/get_toc/get_content` 全部走适配器
4. 适配器异常或返回空 → 自动回退规则引擎（逐方法独立回退）

### 适配器接口（BaseSourceAdapter）
| 方法 | 契约 |
|---|---|
| `search(keyword, page)` | 返回 `[{name, author, intro, kind, cover, book_url, source_uid, source_name}]` |
| `get_book(book_url, fast)` | 返回 dict（name/author/intro/cover/kind/last_chapter/toc_url...）|
| `get_toc(book)` | 填充 `book.chapters`（[{name,url}]）/`book.chapter_count` |
| `get_content(chapter_url)` | 返回清洗后的正文纯文本 |

## 已覆盖源

| 适配器 | 覆盖源 | 解决的站点特性 |
|---|---|---|
| tadu | 塔读文学 | 正文 JS 渲染 → hidden input API；水印段落；UA 反爬 |
| jhsssd | 精华书阁 | 目录分页；章节名首尾混淆噪声字；正文 <br> 分段 |
| biquge_common | 18个笔趣阁模板站 | 目录"最近更新+完整"混合结构；&emsp; 实体 |
| （sfacg 备用）| SF轻小说 | 已删（付费）；Chrome 渲染目录方案保留 |

## 待恢复源（站点级封锁，非代码问题）
| 源 | 现象 |
|---|---|
| 醉读小说 imaxreader.com | RemoteDisconnected（服务器断连）|
| 爱笔楼 m.biqutu.info | RemoteDisconnected |
| 苦读书 kudushu.org | Cloudflare 交互式挑战 |

## 新增适配器步骤
1. `engine/adapters/<name>.py` 实现 `Adapter(BaseSourceAdapter)`
2. `_REGISTRY` 添加 `(域名子串, 模块名)`
3. 测试：`SourceCrawler(source).search/get_toc/get_content` 全链路
