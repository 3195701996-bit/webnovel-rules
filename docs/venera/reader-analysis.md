# Venera 阅读器与漫画能力对照（2026-10-04 复核）

## 范围与来源

- 上游：[`venera-app/venera`](https://github.com/venera-app/venera)，本机隔离参考克隆 `/Users/luoamnke/爬虫系统/opensource/venera`。
- 审阅版本：`a0eba914f4c2a84ac1bc925adec2baabe920b9be`（本次检查时 `master` HEAD）。上游仓库已归档；该仓库 README 标明 GPL-3.0。
- 仓库规模约 48,371 行 Dart。本文是按模块和关键调用链的架构审查，深入读了阅读器、图片加载、下载、本地库、历史、收藏/更新及详情页；不声称逐行审计全部 48k 行代码，也不是法律意见。

## Venera 的核心对象与数据边界

1. `ComicType` 以 source key 的稳定类型值区分本地源与各网络源；同一作品身份由 `(comic id, comic type/source)` 组成，避免不同源的相同 ID 撞车。
2. `ComicChapters` 同时表达普通目录与分组目录，提供统一的 `ids`/`titles` 迭代和分组索引。分组是目录的一等数据，不依赖对章节标题做字符串猜测。
3. `LocalComic` 在 SQLite 本地索引中保存书目元数据、章节目录、下载章节 ID 与磁盘目录；下载图片目录与元数据索引分开，通过 `LocalManager` 的查询/维护 API 联系起来。
4. 阅读历史用 SQLite 保存源身份、当前章/页/组、最近时间及 `readEpisode` 集合。收藏另有独立数据库/文件夹，可区分本地收藏与源站账号收藏；更新检测保存更新时间与上次检查时间。
5. 下载任务是独立可恢复对象，序列化任务状态；下载成功后把章节写入本地漫画记录。缓存与永久离线下载分离：普通图片缓存受 TTL/LRU 类清理管理，不应自动冒充永久下载。

## 阅读器调用链与已确认行为

### 进入与目录

- 本地书库页由 `LocalComicsPage`/`LocalManager` 只枚举设备本地索引；打开本地漫画详情仍走详情页面，但详情优先从 `LocalManager` 取 `LocalComic`，而非用本地元数据覆盖网络详情对象。
- 网络详情从 source 的 `loadComicInfo` 获得完整在线元信息与目录；书目详情用 sliver 文档流，封面、操作区、简介和长章节列表可共同滚动。
- 阅读页入口携带完整 `ComicType`、comic id、章节模型和历史定位。章节分组在目录 UI 中由 tabs/组内列表呈现。

### 本地优先、按章混合在线

- `lib/pages/reader/images.dart` 的 `_ReaderImagesState.load()` 是关键策略：若 `type == ComicType.local`，严格只读取 `LocalManager().getImages(...)`，失败显示本地错误，不回源。
- 对网络源作品，若当前 chapter 在 `LocalManager().isDownloaded(...)` 中，则读永久本地文件；否则调用 source `loadComicPages(cid, chapterId)` 在线读取。也就是“一本作品可本地/在线混合，但每一话在任一时刻选择一条明确的数据路径”。
- 目录页用本地 `downloadedChapters` 给各话标注状态。详情目录保留 source 的完整章节树；本地详情和严格离线入口则以本地记录为准。
- 这与用户定义的目标一致：书库入口是仅本地；在线详情/阅读入口为本地命中优先、缺失章节可在线。实现时应将入口模式显式建模，不应让“本地存在该作品”隐式改变在线详情的目录数据。

### 阅读交互与状态

- Reader 核心 `reader.dart` 聚合页/章导航、分组边界、阅读历史、全屏/方向、按键/音量键与每页多图布局。
- `scaffold.dart` 将内容、顶部栏、底部栏和浮动章导航按钮叠放；顶部/底部栏默认收起，由 `gesture.dart` 的阅读手势控制展开/收起。滑块、页码和页面导航由同一 Reader 定位状态驱动。
- `images.dart` 分别实现连续滚动与 gallery 翻页，含可见页预取、页跳转、图片每页数、多图首屏、缩放及左右/上下阅读模式；`comic_image.dart`/image provider 承担图片渲染与失败反馈。
- 历史定位同时保存章和页；`readEpisode` 额外保存真实读过的章集合，能支持跳读后回头读、已读覆盖非连续的场景。仅用“最后读到第几章”无法复现这一语义。

### 图片、下载与缓存

- `ReaderImageProvider` 接受 `file://` 本地页或网络 image key。网络页通过 `ImageDownloader.loadComicImage`，同一 cache key 的并发请求复用一个 stream；可逐步上报字节进度、取消加载、按 source 配置 headers/响应变换并重试。
- `CacheManager` 以 SQLite 索引缓存文件，支持到期、命中续期、大小上限清理。永久下载写入 `LocalManager` 管理的漫画/章节目录，不与可淘汰缓存混为一类。
- `ImagesDownloadTask` 先取详情/封面/章节页 URL，再受配置的并发数调度图片下载；任务能暂停、取消、恢复、保存进度/路径/已下载位置。任务完成后 `LocalManager.completeTask` 登记本地书目/章节，目录状态由统一索引提供。
- 此模式的关键经验不是无上限提并发，而是清楚分离请求调度、可恢复任务、临时缓存、永久文件和可读性索引，并让下载完成与书架状态更新共享一个提交点。

## 相关源文件的功能地图

| Venera 模块 | 主要职责 | 对本项目的参考价值 |
|---|---|---|
| `lib/foundation/comic_source/` | JS 漫画源接口、模型/章节组、分类、收藏/登录适配 | 适配器输入输出契约、源级能力声明；不是直接移植源脚本运行时的理由 |
| `lib/foundation/local.dart` | SQLite 本地漫画索引、磁盘路径、章节图片枚举、下载状态及删除 | 本地可读目录应作为独立索引/事实源；详情在线模型不能反向决定本地可读性 |
| `lib/foundation/history.dart` | 阅读历史/已读章节集合持久化 | 章节 identity + read set；支持跳读/回读未读计算 |
| `lib/foundation/favorites.dart`、`follow_updates.dart` | 收藏文件夹、本地收藏、阅读标记、更新时间及并发更新检查 | 收藏与下载解耦；更新检查必须限流、可恢复并记录失败 |
| `lib/network/download.dart`、`file_downloader.dart` | 图片/归档任务生命周期、并发、断点/暂停恢复 | 任务状态机与持久化边界；先写完可验证文件再将章节登记为已下载 |
| `lib/network/images.dart`、`foundation/cache_manager.dart` | 共享网络图片流、请求配置、缓存和清理 | 去重、取消、进度、headers、重试和永久下载分层 |
| `lib/foundation/image_provider/` | Reader、缓存、本地漫画、收藏图像等 provider | 本地/网络来源统一渲染接口，但来源与缓存身份仍保留 |
| `lib/pages/reader/` | 阅读定位、界面骨架、章目录、加载、手势、页模式、图片和评论 | 本地优先策略与单一 reader state；小热区唤出控制栏、进度/页码联动 |
| `lib/pages/comic_details_page/` | 漫画详情、章节展示、收藏/下载/分享/评论 | 在线完整详情和章节目录作为在线路径；本地数据只是可读性叠加层 |
| `lib/pages/favorites/`、`local_comics_page.dart`、`history_page.dart` | 收藏/书架/历史的不同集合及批量操作 | 书架、收藏、历史、下载是不同业务概念，不应共用一张“下载列表”语义 |
| `lib/pages/*search*`、`explore_page.dart`、`categories_page.dart` 等 | 搜索/发现、分类、排行、源选择与结果页 | 页面布局与来源聚合方式可借鉴；非核心阅读链路，优先级低于身份与本地阅读正确性 |
| `lib/pages/settings/`、`components/`、`foundation/appdata.dart` | 阅读偏好、主题、源/网络设置、共享组件与持久化 | 交互一致性和用户偏好结构；不宜整体照搬视觉语言 |
| `lib/utils/`、`lib/headless.dart`、平台目录 | CBZ/EPUB/PDF、导入导出、同步、OS 集成、无界面入口 | 可逐一评估用户是否需要；与 2.0 的漫画核心无关时不纳入首轮移植 |

## 与本项目当前设计的对照

| 维度 | Venera | 本项目当前实现 | 2.0.0 决策 |
|---|---|---|---|
| 引擎/界面 | Flutter app 内的 source/plugin + SQLite + 本地文件 | Android Compose + Python 引擎/API；桌面 Web 也调用 Python 服务 | 保留共享 Python source/业务引擎；不移植 Flutter UI/插件运行时 |
| 本地数据 | app 内 SQLite 索引与应用管理目录 | Python `data/manga` 下载/缓存/索引 + Android `OfflineStore` 离线扫描/别名映射 | 服务端索引与 APK 离线扫描继续共存，但共享身份/章节规范化契约并加强校验，避免两套规则漂移 |
| 阅读源选择 | 本地作品严格本地；网络作品已下载章读本地，缺失章在线 | 目标语义已实现：书库本地目录严格本地；在线详情完整目录、阅读按章本地优先/缺失在线 | 保留并用 API 35 端到端断网/混合章节测试锁定，避免把在线 fallback 带进书库模式 |
| 章节模型 | 普通目录或分组目录由 `ComicChapters` 统一表达 | 章节稳定身份、整卷/单章分列并采用“卷先于单章”顺序 | 对齐一个规范化目录模型；API、web、Compose 只投影展示，禁止各端自行排序/匹配标题 |
| 已读/未读 | 最近章页 + `readEpisode` 集合 | 已有跨端 read-set 与未读算法（包含跳章/返回阅读示例） | 保留现有业务逻辑；补身份变更/卷内多话/新增最新话迁移边界，不照抄 Venera DB schema |
| 下载完成状态 | task 完成时登记 LocalComic 的下载章节 | 下载目录/清单/真实有效图片/章节身份扫描共同确认，且有旧别名兼容 | 继续把“可读文件+明确身份+完整页集”作为已下载真值，索引更新与任务完成采用原子/可恢复流程 |
| Reader UX | 多模式阅读、导航、手势、页进度/章节状态 | 已具纵向/横向、中心小热区显隐栏、实时可拖页滑杆、旋转保位、实体键支持 | 借鉴其布局与功能边界而非大改结构；探索更平顺连续阅读/缩放/预取应另立可测验收 |

## 采用建议

**优先自制（沿用当前原生架构）**：Venera 与本项目都是长篇图像阅读，但其 UI/数据运行时是 Dart/Flutter；逐块搬入 Kotlin/Compose 或 Python 会引入双模型、双生命周期和授权/发布负担。最有价值的是行为模型：本地书架严格离线、在线作品逐话本地优先、read-set、章节组为一等模型、下载任务的状态机、缓存和永久下载分离。

**有条件的小范围移植**：仅当确认确需功能且评估 GPL-3.0 合规、保留版权/许可证并满足分发源码义务后，才考虑直接复制可独立识别的算法/实现片段。当前不建议移植 Venera Dart UI、JS source engine、SQLite schema 或网络下载器。上游许可证状态须由项目负责人/法律顾问对整体分发模式作最终判断。

## 后续 2.0 落地项

1. 以固定 synthetic manga fixture 验收四类入口：书库只见本地章且断网可读；搜索/历史完整在线目录；已下载章本地优先；未下载章在线打开/失败提示/可加入下载。
2. 定义唯一跨端 `ComicIdentity`、`ChapterIdentity`、volume/chapter 序与 `ReadProgress` 数据契约；用真实旧数据 fixture 覆盖 CopyManga 双通道 alias、章节 ID 改变、卷内多话和目录更新。
3. 建立下载事务：写临时页文件→校验 magic/页码/预期页数→原子落盘→更新持久清单/书库快照→通知 UI；中断和 ENOSPC 后既不误报已下载也不能丢弃已有章节。
4. 分别比较 Venera 与当前实现的内存/预取/页面模式体验。只在基准确认瓶颈且源站策略允许时调并发/节流；避免把 Venera 的 source 网络行为直接套在各漫画站。
5. 用户确认的阅读控制规则继续作为约束：只有阅读时自动隐藏；中央较小触区才切换控制栏；进度条实时反映页/章节且可快速跳转。

## 主要代码锚点

- Venera 阅读路径：`lib/pages/reader/reader.dart`、`images.dart`、`scaffold.dart`、`gesture.dart`、`chapters.dart`、`comic_image.dart`。
- Venera 本地/下载/缓存：`lib/foundation/local.dart`、`lib/network/download.dart`、`lib/network/images.dart`、`lib/foundation/cache_manager.dart`。
- Venera 身份/历史/收藏：`lib/foundation/comic_type.dart`、`lib/foundation/comic_source/models.dart`、`lib/foundation/history.dart`、`lib/foundation/favorites.dart`、`lib/foundation/follow_updates.dart`。
- 当前 APK：`android/app/src/main/java/com/webnovel/mobile/ReaderScreens.kt`、`MangaScreens.kt`、`OfflineStore.kt`、`MainActivity.kt`。
- 当前共享引擎：`engine/manga/`、`server/manga_api.py`；相关测试分布在 `tests/test_manga_*` 与 Android `*Manga*Test.kt`。
