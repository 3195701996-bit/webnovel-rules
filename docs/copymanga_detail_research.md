# 开源项目详情页实现研究 + 拷贝漫画防爬落地（2026-08）

研究对象：venera-app/venera（Flutter/Dart，JS 源驱动）、Kototoro-app/Kototoro
（实为 **Kotatsu 的 Kotlin 社区续作**，kotatsu-parsers + Room）、
Haleydu/Cimoc（Java，GreenDAO 本地缓存）、LittleSurvival/copymanga-copy20
（Mihon 扩展，拷贝漫画防爬成熟方案）。

## 一、开源项目详情页实现对比

| 项目 | 语言/架构 | 详情数据组织 | 缓存 | UI 结构 |
|---|---|---|---|---|
| venera | Flutter + QuickJS 源脚本 | ComicDetails: title/subTitle/cover/desc/**tags(Map 分组)**/chapters(Map 组名→列表)/recommend/isLiked/likesCount/views | SQLite 历史/收藏；图片磁盘缓存；**详情不缓存** | CustomScrollView+Sliver：封面+标签 chips+折叠简介+分组分段选择器+章节圆角按钮流（已读置灰） |
| Kototoro/Kotatsu | Kotlin + kotatsu-parsers | Manga: id/title/altTitle/rating/isNsfw/coverUrl/**tags:Set**/state/author/source；MangaChapter: id/name/**number(Float)/volume(Int)**/scanlator(汉化组)/uploadedAt/**branch(分组)** | Room(HistoryDao/FavouritesDao)；详情仅 HTTP 缓存兜底 | DetailsActivity(Compose)：封面头图+信息卡+收藏(分类)/继续阅读/下载/浏览器打开；章节按 branch 分组(Spinner)+正倒序+已读降透明度+新章角标 |
| Cimoc | Java + RxJava2 | Comic: source/cid/title/cover/update/author/intro/last/finish/favorite/history/download（**无 tags**） | **GreenDAO 本地优先** | DetailActivity 折叠封面头+信息+Grid 章节；加载=ProgressBar，失败=重试 |
| copymanga-copy20 | Kotlin (Mihon) | SManga 标准字段 | Mihon 应用层 initialized（手动刷新才重拉） | Mihon 标准详情页 |

**venera 详情数据模型 ComicDetails 字段清单：**
title / subTitle / cover / description / tags(Map<String,List<String>>) /
chapters(Map<String,List<Chapter>>) / recommend(List<Comic>?) /
isLiked / likesCount / commentsCount / uploader / updateTime / views

**关键发现：拷贝漫画 API 软限制 ≈ 15 次/分钟/IP**（lanyeeee/copymanga-downloader issue 多次记录 HTTP 210）
→ 我们 RateGuard normal 4s/请求 = 15/min，**精确对齐** ✅

**copymanga-copy20 防风控四件套（我们已全部落地）：**
1. 域名轮换表：mangacopy.com → 2025copy → copy3000 → …（210 触发换域）
2. `authorization: Token` 头 + App 指纹伪装（COPY UA/source/platform/deviceinfo）
3. 按域名限速 ~1 req/s
4. 210 响应 → 换域名退避重试

**章节获取**：详情 1 次 comic2（元信息+groups 分组+每组 count）+ 每组
`/chapters?limit=500&offset=` 分页；图片 words 乱序重排
（`ordered[i] = contents[words.index(i)]`，我们实现等价验证 ✅）

## 二、我们的落地实现（engine/manga/）

### copymanga.py（APP API）
- 域池 5 域名（新增 api.mangacopy.com / api.copy3000.com，实测可用）
- 章节 limit=500 一次拉取（对齐 Mihon，请求数减半）
- comic2 响应解析 recommend（有则展示）
- 指纹轮换 + HMAC 签名 + request_id + 动态版本 + 风控状态机
- RateGuard 4s/请求（= 15 次/分钟软限制对齐）

### copymanga_web.py（网页版，Playwright）
- 详情页提取：标题/封面/简介 + **作者/标签/最后更新**（DOM 正则）
- 推荐区提取：/comic/ 链接去重
- 单 worker 线程池（Playwright 线程安全根治）

### app.py（Flask 层）
- 详情 **SWR 缓存**（7 天 TTL；过期先返旧+后台刷新，对齐 Mihon"不重拉"）
- 风控敏感期先走网页降级（跳过徒劳 API 重试链）
- 404（下架）与 502（风控）**错误区分提示**
- 所有 URL 路径参数 _safe_seg 校验

### 前端（对齐 Kotatsu）
- 详情页章节**已读置灰 + ✓ 标记**（history 表驱动，同时支撑"继续阅读"）
- 推荐区网格（源支持时）
- 404/超时/风控 三类错误提示区分

## 三、详情页展示（对齐 7 元素 UI 规格）

顶部导航(返回/更多) → 封面+标题+副标题 → 操作区(开始/点赞/收藏/评论)
→ 功能区(下载/继续阅读) → 阅读进度提示 → 信息区(作者/标签/浏览/更新)
→ 章节列表(分组, 已读置灰) + 推荐区（源支持时）

字段验证（copymanga 海贼王）：作者 尾田栄一郎 / 标签[2] / 浏览 1274万 /
更新 2026-08-12 / 395 章 ✅
字段验证（copymanga_web 情書）：作者 村上香 / 标签[愛情,校园] / 更新 2026-02-15 ✅

## 四、防爬现状与结论

- IP 级"破解版"标记（TTL≈1h）是硬限制：高频详情请求（6+ 请求/详情）会触发
- 缓解：limit=500 减半请求数、SWR 缓存（详情请求压到最低）、多域名轮换、
  网页降级通道、Token 认证（用户可配置）
- 低频正常使用不触发；触发后系统自愈（cooldown→探测→恢复）
