# venera 漫画详情页与图片加载设计研究（阶段四参考）

来源：venera-app/venera（Flutter，已停止维护）源码分析
- `lib/pages/comic_details_page/comic_page.dart`（详情页 1068 行）
- `lib/pages/comic_details_page/actions.dart`（操作区）
- `lib/pages/comic_details_page/chapters.dart`（章节列表）
- `lib/foundation/comic_source/models.dart`（数据模型）
- `lib/foundation/image_provider/reader_image.dart`（阅读器图片）
- `lib/network/images.dart`（图片下载器）

## 1. 详情页信息架构（自上而下）

1. **SliverAppbar**：滚动时标题淡入（AnimatedOpacity），右上更多按钮（复制标题/复制ID/复制URL/浏览器打开）
2. **标题区**：封面（144×104 圆角阴影，点击全屏查看 cover_viewer，长按保存）+ 标题（SelectableText 可复制）+ 副标题 + 源名
3. **操作区**：继续阅读（有历史时 ep>1||page>1）、开始阅读、收藏（多文件夹）、下载（含章节选择）、评论、评分（星级）
4. **简介区**：可展开收起
5. **信息区**：彩色标签（类型/状态，点击查看该类漫画+长按复制）、上传者、上传时间、最大页码
6. **章节区**：分组模式（Tab 切换卷/组）或普通列表；可反转顺序（设置）；长按章节菜单
7. **评论预览 + 缩略图 + 推荐漫画**（网格）
8. **FAB**：回到顶部

## 2. 数据模型 ComicDetails

title / subTitle / cover / description / tags(Map<分组,List>)
chapters(ComicChapters: flat Map 或 grouped Map) / thumbnails / recommend
isFavorite / subId / isLiked / likesCount / commentCount
uploader / uploadTime / updateTime / url / stars / maxPage / comments

## 3. 章节模型 ComicChapters

- 扁平：`Map<章节名, 章节URL>`
- 分组：`Map<组名, Map<章节名, 章节URL>>`（卷/话分组）
- 前端 Tab 切换分组

## 4. 图片加载/防盗链（下载与阅读共用）

核心：**每源可配置图片加载行为**（`getImageLoadingConfig(imageKey, cid, eid)` JS 回调）：
- 返回 `{url?, method?, data?, headers?}` 覆盖默认请求
- 默认 headers 仅 user-agent（webUA），源可自定义（Referer/Cookie/token/签名）
- `onLoadFailed` 回调：加载失败后可重新生成配置（应对 token 过期）
- `onResponse` 回调：图片字节后处理（解密/裁剪）
- **重试 5 次**；流式进度（cumulativeBytesLoaded/totalBytes）
- 缓存：CacheManager 键 `url@sourceKey@cid@eid`，命中直接返回
- 缩略图单独配置（getThumbnailLoadingConfig）
- 封面懒加载：`cover.` 前缀 → 先 loadComicInfo 拿真封面 URL
- 自定义图片处理脚本（JS processImage 对字节后处理）

## 5. 下载任务

- ImagesDownloadTask（图片逐张）+ ArchiveDownloadTask（整包下载，如 cbz/epub）
- 下载状态查询：isDownloading/isDownloaded(comicId, type, group)
- 下载进行中再次点击 → 提示"正在下载"

## 6. 对 webnovel_rules 漫画板块的设计启示

1. **漫画源规则**：ruleSearch（搜索列表）、ruleToc（章节列表，支持分组）、ruleContent 返回图片 URL 列表（@css:img@src 多元素）
2. **图片加载配置必须每源化**：适配器实现 `get_image_headers(image_url, comic_id, ep_id) -> {headers, url?}`，支持动态刷新（onLoadFailed）
3. **下载与阅读共用下载器**：缓存键 `image_url@source@cid@eid`，断点续传
4. **详情页字段**：封面/标题/作者/简介/标签/最新章节/更新日期/章节数/收藏数
5. **阅读器**：翻页模式（单页/卷纸），图片预加载 + 进度显示
6. **拷贝漫画适配**：图片 headers 需 Referer（图片域）+ UA（COPY/3.0.6）+ 动态签名
