# Venera 全项目功能拆解与 2.0.0 适配决策

检查日期：2026-10-04
上游：`https://github.com/venera-app/venera`，本机参考克隆 `/Users/luoamnke/爬虫系统/opensource/venera`
检查基线：`a0eba914f4c2a84ac1bc925adec2baabe920b9be`（`origin/master` 同一提交）

## 结论先行

Venera 是 Flutter 漫画阅读应用，而本项目是 Python 本地服务/采集引擎 + Android Compose 客户端 + 桌面/移动 Web 的跨端系统。两者产品交集集中在漫画源、目录、阅读器、下载、收藏、阅读历史和本地媒体；Venera 还包含本地压缩包导入、评论/评分、图片收藏、WebDAV 同步、JS 源运行时、多平台桌面壳等，本项目没有必要为了“像 Venera”而整体照搬。

本项目已独立实现了大部分漫画阅读核心语义，且有若干更严格的约束：书架入口只读已验证的本地章节；在线入口保留完整目录并逐章优先本地；章节 ID 而非序号作为阅读/未读身份；媒体缓存不等于正式下载；搜索页头部随结果滚动；阅读控件仅由小中心热区唤出。上述差异是产品契约，应保留。

建议采用“行为借鉴、按本项目栈独立实现”。Venera 根目录声明 GPL-3.0；若复制、改写或把其代码合并进 APK，会引入许可证分发义务。本项目目前未复制 Venera 实现、Dart 代码、SQLite schema 或依赖。

## 上游仓库与分析边界

- 已在独立克隆 `/Users/luoamnke/爬虫系统/opensource/venera` 执行 `git fetch origin --prune`；本地 HEAD 与 `origin/master` 均为 `a0eba914f4c2a84ac1bc925adec2baabe920b9be`，没有上游新提交。
- 该克隆当前存在用户未提交改动（`android/settings.gradle`、`lib/foundation/comic_source/comic_source.dart`、`pubspec.lock`、`pubspec.yaml`，以及 `android/build/`、`assets/copymanga_source.js`）；本次没有清理、覆盖或构建这些内容。分析基于审阅过的 `a0eba914` 提交，不把工作树差异当作上游正式代码。
- README 表明项目已停止维护，故它适合作为源码行为样本，不是当前源站兼容性或安全维护基线。
- 下表按每个源码文件承担的职责归类；同一业务由多个文件协作，避免把文件名误解为可独立移植组件。

## Venera 整体架构

| 子系统 | 主要源码 | 责任与数据流 | 对本项目的映射 |
|---|---|---|---|
| 启动与应用壳 | `lib/main.dart`、`init.dart`、`headless.dart`、`foundation/app.dart`、`appdata.dart`、`global_state.dart`、`context.dart`、`app_page_route.dart` | 初始化平台目录、设置、JS 运行时、数据库和路由；维护应用生命周期、全局状态与桌面/移动布局入口；headless 模式提供无 UI 的操作入口。 | Android `MainActivity.kt`/`LocalServerService.kt` 与 Python `server/runtime.py` 启动本机服务；Web 有独立入口。应借鉴启动阶段可观测性和状态边界，不引入 Flutter app shell。 |
| 主导航和首页 | `pages/main_page.dart`、`home_page.dart`、`history_page.dart`、`local_comics_page.dart`、`downloading_page.dart` | 主导航组织首页、收藏、探索、分类；首页聚合搜索、历史、本地作品、追更、源入口、图片收藏、同步；历史和下载列表提供继续阅读/任务入口。 | Android `MainActivity.kt`、`HistoryScreens.kt`、`DownloadScreens.kt`；Web `index.html`、`library.html`、`tasks.html`。可借鉴首页按阅读任务组织，而非工程诊断状态堆叠。 |
| 漫画发现 | `pages/search_page.dart`、`search_result_page.dart`、`aggregated_search_page.dart`、`categories_page.dart`、`category_comics_page.dart`、`explore_page.dart`、`ranking_page.dart` | 搜索历史、单源/聚合搜索、分类标签和分类作品、源提供的发现页/排行榜；探索页支持按源拼装单页、混合页及多分区，并记住滚动位置、刷新当前分区。 | Android `SearchScreens.kt`、`ExploreScreens.kt`、`MangaSourceScreens.kt`；Web `manga.html` 与搜索/浏览 API。适合借鉴每源能力声明、搜索与发现的渐进加载；保留用户已要求的全页滚动，不固定搜索表单。 |
| 漫画源插件 | `foundation/comic_source/{comic_source,parser,models,types,category,favorites}.dart`、`foundation/js_engine.dart`、`foundation/js_pool.dart`、`components/js_ui.dart` | JS 插件注册源能力：搜索、详情、分类、发现、图片、登录、收藏、评论/评分等；QJS 引擎提供网络/DOM/脚本 API、隔离池及 JS 自定义 UI。 | `engine/manga/*.py` 以 Python adapter 实现源；`server/manga_api.py` 聚合统一 API；Android/Web 消费字段契约。值得借鉴的是显式能力矩阵、单源错误隔离和源级配置，不把可执行 JS 插件/QJS runtime 嵌入 APK。 |
| 漫画详情与元数据 | `pages/comic_details_page/{comic_page,actions,chapters,favorite,cover_viewer,thumbnails,comments_page,comments_preview}.dart` | 展示详情、分组目录、标签/评分/描述、继续阅读、收藏、批量下载、封面/缩略图及作品/章节评论；详情行为取决于源能力。 | Android `MangaScreens.kt`/`MangaSourceScreens.kt`，Web `manga_detail.html`、`manga_download.html`，服务端详情 API。借鉴信息分层和长目录定位；评论/评分依赖真实源能力，未列入 2.0 必做项。 |
| 阅读会话与输入 | `pages/reader/{loading,reader,images,chapters,gesture,scaffold,comic_image,chapter_comments}.dart` | `loading` 解析在线/本地目录与续读；`reader` 持有作品/章/页/模式状态；`images` 实现连续与分页编排、邻页加载、章边界；`chapters` 目录；`gesture` 触控/鼠标/键盘/音量输入；`scaffold` 上下栏、Slider、设置、章节导航；图片及章节评论由独立模块承接。 | Android `MangaScreens.kt`、`ReaderPrefs.kt`；Web `manga_reader.html`；Python 目录/图片/历史 API。可复用行为规格，不复用 Flutter 手势和阅读会话代码。细节见 `docs/venera-reader-source-review.md`。 |
| 下载队列和本地媒体 | `network/download.dart`、`network/file_downloader.dart`、`foundation/local.dart`、`foundation/comic_type.dart`、`pages/downloading_page.dart`、`utils/cbz.dart` | 可序列化图片下载任务，管理并发、暂停/取消/恢复、速度和进度；逐话图片与归档下载路径分开；本地管理器保存作品/章节/已下载身份；CBZ/ZIP/7z 导入、目录注册、导出和批量管理。 | Python `engine/manga/download_manager.py`/`downloader.py`、服务端磁盘扫描及删除/续传 API；Android `OfflineStore.kt`/`DownloadScreens.kt`；Web task pages。借鉴任务恢复与“章节清单 + 媒体文件”双重状态；本项目严格以可读实盘认定下载。压缩包导入是新增产品范围，建议 v2.0.0 后独立评估。 |
| 阅读历史与作品收藏 | `foundation/history.dart`、`foundation/favorites.dart`、`foundation/follow_updates.dart`、`pages/favorites/*`、`pages/follow_updates_page.dart` | SQLite 历史保存章节组/序号、页和已读集合；收藏支持本地/网络作品、文件夹与批量操作；追更遍历收藏、刷新详情并比较源更新时间。 | 服务端 `manga_api.py` 的 canonical source/comic/chapter 身份、阅读进度/收藏/更新 API；Android 与 Web 展示。借鉴收藏与历史分离、更新任务可见；不采用 Venera 序号型已读身份或单纯用更新时间计算未读。 |
| 图片收藏 | `foundation/image_favorites.dart`、`foundation/image_provider/image_favorites_provider.dart`、`pages/image_favorites_page/*` | 阅读时保存单张图片及来源/作品/章节关联；独立浏览和图片查看器。 | 当前项目没有等价的完整图片收藏库。该功能增加媒体容量、删除语义和备份范围，用户尚未提出；列为后续可选功能，不纳入 2.0 核心闭环。 |
| 网络、缓存与源保护 | `network/{app_dio,cache,cookie_jar,cloudflare,proxy,images}.dart`、`foundation/cache_manager.dart`、`foundation/image_provider/*` | 统一请求客户端、代理/TLS/DNS/cookie、Cloudflare 流程、网络响应缓存、图片请求合并/取消/传输进度、磁盘缓存 TTL/限额和图片提供器。 | Python `engine/netproxy.py`、适配器、`server/state.py` 缓存与单飞、图片代理；Android Coil 与本机引擎连接。采纳缓存/正式下载分离、同图请求去重、取消和可诊断错误；不直接套用固定重试次数/缓存期限。 |
| 设置与视觉组件 | `pages/settings/*`、`components/{appbar,button,comic,image,layout,scroll,select,menu,loading,message,gesture,side_bar,window_frame,code,js_ui}.dart`、`foundation/{consts,res,widget_utils}.dart` | 阅读偏好、网络/代理、探索页、外观、收藏、调试等配置；共享列表/网格/滚动、图片卡片、按钮、菜单、弹层、加载/错误、代码编辑器和桌面窗口装饰。 | Android `WnTheme.kt` 及各 Compose Screen、Web `static/css/style.css`/模板。借鉴响应式布局、滚动复用、空/错误/加载状态设计；UI 必须按 Android Compose 和 Web 设计系统重做，避免调试面板式诊断文案进入普通阅读路径。 |
| 数据迁移/同步/导入导出 | `utils/{data,data_sync,import_comic,cbz,epub,pdf,file_type,io}.dart` | 应用数据归档导入导出；WebDAV 自动同步，含字段排除、版本文件轮转和冲突保护；从目录、CBZ、EhViewer 等导入本地作品；多格式文件处理。 | Android `Backup.kt`/`Export.kt` 已覆盖本地设置/收藏等备份恢复，且明确排除漫画媒体；还没有等价的跨设备 WebDAV 同步或本地压缩包书目导入。恢复事务安全和字段范围可借鉴；WebDAV 凭据/冲突策略和导入语义复杂，单独设计，不直接迁移实现。 |
| 辅助能力 | `utils/{app_links,channel,clipboard_image,image,opencc,tags_translation,translations,volume,ext}.dart`、`pages/{auth_page,comic_source_page,webview}.dart`、`foundation/{log,res}.dart` | 深链接/分享/平台桥接；剪贴板图像、字体/标签翻译、繁简转换、音量翻页、国际化、源管理/账号授权、内嵌网页和日志。 | Android `SourceScreens.kt`、`NetState.kt`、`Diagnostics.kt` 及 Web 源管理。借鉴非敏感日志脱敏和无障碍输入；音量键、标签翻译、WebView 登录按实际需求/源许可再评估。 |

## 与本项目的功能映射和取舍

| Venera 能力/特点 | 我们已有的对应能力 | 结论 |
|---|---|---|
| 网络漫画源和源级功能 | Python adapter、源管理、API 字段契约、Android/Web 搜索/浏览 | 保留 Python 为源执行事实层；补能力声明/可用性状态，而不是引入 JS 执行环境。 |
| 本地作品与网络作品 | `catalog=local` 严格本地目录/图片；在线目录允许本地优先、未下载时回源 | 保留两个入口语义，禁止用 Venera 网络书目回源规则模糊本地书架。 |
| 下载任务恢复 | Python 可恢复队列/检查点/限流；端上任务展示及暂停恢复 | 继续强化完整性与失败恢复；上游的 `_totalCount` 式单一进度不能代表章节完整。 |
| 连续/分页、缩放、目录、进度 | Android 与 Web 已有独立实现、中心窄热区和自动隐藏 | 按用户指定保留“小中心点击才显示”；不照搬任意阅读画布点击唤栏。跨端黄金样本继续共享。 |
| 收藏/历史/更新 | 跨端 canonical identity、稳定 chapter ID、跳读未读集、启动更新检查 | 现有语义优先于 Venera；保留卷优先、章节插入后的身份恢复。 |
| 卡片式发现/空状态 | 首页、搜索、浏览模板与 Compose 页面 | 有设计借鉴价值；不需要移植控件代码。继续迭代视觉系统、加载/空/失败状态及长列表性能。 |
| 本地压缩包导入/漫画导出 | 尚无完整的本地 CBZ 作品管理器；当前 ZIP 导出主要针对已下载漫画 | 可作为后续方向，但会引入 SAF 授权、压缩炸弹/路径穿越防护、重复导入、章节排序、备份和阅读历史身份；不是 2.0 发布前顺手加入的内容。 |
| 多设备 WebDAV 同步 | 已有设备本地备份/恢复和后端/端上数据结构 | 不建议直接照搬：必须先定义冲突合并、加密、凭据保护、删除传播、媒体不上传与数据 schema 版本。可另立需求。 |
| 图片收藏、评论/评分、源 JS UI | 没有完整对应业务或并非各源都支持 | 暂不纳入；避免扩张发布范围，优先把阅读/下载/更新/恢复做可靠。 |

## 2.0.0 应采用的实现准则

1. Python 服务端是跨端漫画目录、下载实况和稳定身份的唯一事实源；Android 不从路径布局猜下载状态，Web 不把图片缓存当已下载。
2. 严格本地与在线混合阅读是两个显式目录策略：前者零源站请求；后者完整卷/章目录、本地命中优先、缺章才请求网络。
3. 阅读进度以 `(identity_source, comic_id, chapter_id, page)` 保存，目录索引只作兼容；卷内话和独立单话顺序由统一规范器决定。
4. 网络图片可见加载优先；相邻预取有界、可取消、按会话代际校验；源站超时/限流与服务端/客户端错误分类一致。
5. 收藏更新角标由当前章节身份与实际已读集合差集计算；源更新时间只负责排序/显示，不代替未读语义。
6. 普通 UI 展示用户任务语言；源诊断、API 细节放在自愿打开的诊断页面。
7. 接收任何上游衍生代码前先做许可证/依赖审查；默认只实现独立的行为兼容，不复制实现。

## 下一步顺序

1. 以本文件和 `docs/venera-reader-source-review.md` 作为行为规格，逐项做旧数据/故障场景审计，而不是整批移植。
2. 2.0 优先收敛：真实旧下载目录识别与增量下载、长目录滚动/当前章定位、进程中断/低存储恢复、Android/Web/服务端状态一致、目标设备真实源闭环。
3. CBZ 导入、WebDAV、图片收藏等新范围另行评估；它们不能挤占已经确认的升级修复和发布门槛。
4. 未完成目标设备、旧版覆盖升级和当前 SHA CI 前不构建/发布 2.0.0 release。
