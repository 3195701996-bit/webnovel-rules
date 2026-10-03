# Venera 漫画阅读功能源码拆解与本项目对照

日期：2026-10-04（本次复核）
参考仓库：venera-app/venera，本机 `/Users/luoamnke/爬虫系统/opensource/venera`，origin/master = a0eba914f4c2a84ac1bc925adec2baabe920b9be（2026-04-05）
用途：为本项目 2.0.0 的 Android APK、桌面 Web、Python 引擎与漫画数据契约提供实现参考；不是直接移植清单。

## 仓库状态与边界

- 上游 `lib/` 共 135 个 Dart 文件、约 48,381 行；全仓按子系统梳理职责，阅读/本地库/下载/历史收藏主链进一步逐文件分析。这里不是对 48k 行逐行代码审计，也不会将模块映射冒充逐行验证。
- 已对本地 Venera 仓库执行只读 git fetch，远端 master 与本地 HEAD 相同，无待拉取提交。
- 2026-10-04 再次 fetch 后 HEAD 与 `origin/master` 一致；当前工作树有用户未提交文件与构建产物，本次未清理、覆盖或构建，审阅仍以 `a0eba914` 为准。
- 仓库 README 明确写明项目停止维护；版本快照时间为 2026-04-05，不能把它视为持续更新的兼容性基准。
- Venera 根目录的 LICENSE 是 GPL-3.0。若把其实现代码改写/移入并随 APK 分发，必须先处理许可证兼容、修改标记、源码提供等义务。本项目优先采用“参考行为、按现有 Kotlin/Python/HTML 架构独立实现”；本文件不构成法律意见。

## Venera 各功能层做什么

| 层/源码 | 职责 | 关键行为 |
|---|---|---|
| lib/foundation/comic_source/ | JS 漫画源插件接口、解析与运行时注册 | ComicSource 聚合搜索、详情、分类、目录、图片、收藏、登录、评论等回调；ComicSourceParser 把 JS 源注册到 JS 引擎。ComicChapters 同时表示扁平目录和按卷/组分组目录，扁平化时保留 Map 插入顺序。 |
| lib/foundation/comic_source/models.dart | 漫画领域模型 | Comic / ComicDetails 携带标题、封面、标签、源 ID、详情、目录、缩略图、更新时间等。章节分组保留源数据结构，不自行推断卷与话的语义。 |
| lib/pages/reader/reader.dart | 阅读会话状态机 | 保存源/漫画/章节/页/组、方向、当前模式、最大页数；支持进入指定页、组章节定位、切话、历史写入、旋转后重新计算每屏页数、内存图像缓存按可用 RAM 调整。 |
| lib/pages/reader/images.dart | 两种阅读布局及图片编排 | Gallery 分页模式支持左右/右向左/上下、单页/多图、翻页动画；Continuous 模式支持纵向或横向长卷；预载附近图片；边缘过度滚动提示并可切换上一/下一话；章末评论可作为附加页。 |
| lib/pages/reader/gesture.dart 与 comic_image.dart | 手势和图像显示 | 点击翻页/开关控制栏、双击缩放或快速收藏、长按缩放、触控/鼠标/键盘/音量键翻页，图片点击定位、保存、复制及错误重试。阅读模式会影响翻页手势解释。 |
| lib/pages/reader/scaffold.dart 与 chapters.dart | 阅读器控制和目录 | 顶栏包含标题/返回/章节入口；底栏含实时页码 Slider、前后章节、设置、旋转、全屏、定时翻页、收藏/保存/分享；目录可平铺或分组 Tab，展示已下载状态并可反转排序。控制层初始隐藏，触碰阅读画布后切换显隐。 |
| lib/foundation/image_provider/、lib/network/images.dart | 在线与本地图像加载 | 阅读器图片、封面/缩略图、本地漫画分别有 Provider；在线图片按源获得 URL/请求头/方法/请求体，可在失败时刷新配置、在响应后处理字节；最多重试 5 次；按 URL+源+漫画+章节缓存、合并同图并发请求、提供传输进度。 |
| lib/foundation/cache_manager.dart | 图片/网络缓存索引 | SQLite 保存缓存 key、相对文件位置和过期时间；命中刷新有效期；过期清理并在超限时淘汰缓存。阅读图片缓存与用户正式下载内容不是同一个存储语义。 |
| lib/foundation/local.dart、lib/pages/local_comics_page.dart | 本机漫画目录、下载记录和任务队列 | SQLite 保存本地书目、来源、目录、章节元数据、已下载章节 ID；本地列表可排序/搜索/批量选取、导出、删除书目或删除所选章节；下载任务持久化恢复、暂停、重排和继续。 |
| lib/network/download.dart、file_downloader.dart | 图片逐张下载与归档下载 | ImagesDownloadTask 逐话取图片列表、逐图落盘并构建 LocalComic；ArchiveDownloadTask 下载 CBZ/ZIP 等归档并解压。下载状态由任务状态和 downloadedChapters 两部分维护。 |
| lib/foundation/history.dart | 阅读历史与已读集合 | SQLite 记录章节/页/组、时间、最大页、readEpisode 集合；阅读时异步落盘，收藏历史可保留；历史信息可从源刷新而不覆盖阅读位置。进度以章节序号/组序号为主，非稳定章节 ID。 |
| lib/foundation/favorites.dart、follow_updates.dart、lib/pages/favorites/ | 收藏夹、收藏夹文件夹、更新检查和图片收藏 | 支持网络收藏、多个收藏夹、本地收藏、批量操作；更新检查刷新详情并比较源更新时间，流式输出处理进度；另有阅读中图片收藏。 |
| lib/pages/comic_details_page/ | 作品详情信息架构 | 封面、标题、标签、简介、元信息、继续/开始阅读、收藏、章节下载、分组目录、评论、缩略图和推荐；详情目录允许卷组切换。 |
| lib/pages/settings/reader.dart | 阅读偏好 | 支持设备级及漫画/源级覆盖；阅读方向、连续/分页、每屏页数、预载张数、双击缩放、点击翻页等设置。 |

## 阅读数据链路（实际源码语义）

 详情/书目提供 ComicDetails + ComicChapters
              │
              ├─ 阅读某章时 LocalManager.isDownloaded(...) 为真
              │      └─ LocalManager.getImages(...) → 本机文件 URI
              │
              ├─ ComicType.local（导入的本地作品）
              │      └─ 本机文件 URI；不会调用在线源
              │
              └─ 网络源作品该章未下载
                     └─ ComicSource.loadComicPages(...) → 源站图片键/URL
                            └─ ReaderImageProvider → ImageDownloader → 源定制请求/缓存/重试

因此，Venera 的网络源“本地书库”并非严格的本地目录模式：LocalComic.read() 把保存的章节目录传给同一个 Reader，而 _ReaderImages.load() 对“该章尚未下载”的网络源分支仍调用源站 loadComicPages()。只有 ComicType.local 的导入内容是严格本地。用户要求的“书架详情只显示本地已下载目录；在线详情显示完整目录，在线阅读本地优先并可读未下载章节”应当作为本项目自己的明确产品规则，不要照搬 Venera 这个入口语义。

## 文件级职责与协作边界

这里按阅读主链逐个列出核心文件，而不是把 Venera 整个应用的每个页面都纳入移植范围。该边界避免把与本项目目标无关的图片社区、评论、同步或 Flutter UI 一并引入。

| Venera 文件 | 文件承担的具体职责 | 与本项目对应位置 / 决策 |
|---|---|---|
| `pages/reader/loading.dart` | 进入阅读前解析在线源或本地作品、取阅读历史并组装 Reader 参数；在线源优先加载实时详情，本地源从 LocalManager 读取保存目录。 | Android `MangaScreens.kt`、Web `manga_reader.html` 和服务端详情/历史 API；本项目继续明确传递 `catalog=local`，不能因“有网络 source”而让本地书架偷偷回源。 |
| `pages/reader/reader.dart` | 阅读会话的权威状态：作品/章节/组/页、模式、旋转变化、页数计算、已读/收藏触发、图片缓存预算；把 Scaffold、Images、Gesture 等拆成 part。 | Android `MangaScreens.kt` / `ReaderPrefs.kt`、Web `manga_reader.html`；采用稳定章节身份和页位置，不采用 Venera 以 ep/group 序号作为持久身份的做法。 |
| `pages/reader/images.dart` | 管理当前话图片加载状态及错误；实现 Gallery 分页与 Continuous 连续阅读两种视图、当前页定位、邻页预取、双击/长按与跨章边界。 | Android Compose 阅读屏、Web reader 的 `setMode`/分页渲染与预取；可复用的是“可见图片优先、邻接预取可取消”的原则，不复用 Flutter widget/固定预载数量。 |
| `pages/reader/gesture.dart` | 将单击分区、双击、长按、拖动、鼠标滚轮、右键和保存/复制图片手势统一路由，并处理手势冲突。 | Android Compose pointer/key handlers 与 Web pointer/keyboard handlers 各自实现；本项目保留用户指定的小中心热区，禁止整屏任意点击都唤出栏。 |
| `pages/reader/scaffold.dart` | 阅读器外框和上下控制层；目录入口、页数 Slider、前后话切换、设置/旋转/全屏、章节评论、图片收藏/保存/分享及状态信息。 | Android/Web 阅读控件和可拖动实时进度条；只采纳核心导航与 Slider 行为，系统状态栏、电量时钟、评论、剪图工具按产品优先级单独决定。 |
| `pages/reader/chapters.dart` | 目录列表与分组目录 UI，支持组/章节选择、排序方向、章节项和下载标记。 | Android Compose 与 Web 目录组件；数据顺序由服务端规范化（卷在前、卷内话、单章在后），当前项定位/跨批次目录不能只依赖 UI 列表序号。 |
| `pages/reader/comic_image.dart` | 包装 Flutter ImageStream 生命周期，监听解码帧/下载字节进度、图像尺寸、颜色反转及 widget 更新/销毁。 | Android Coil/Compose 与 Web `<img>`/请求状态分别实现；按需保留可见加载状态和解码失败恢复，不移植 ImageProvider 实现。 |
| `foundation/comic_source/{comic_source,parser,models,types}.dart` | 定义漫画源插件接口、注册解析、源能力类型、作品/章节/分组模型；源可实现搜索、详情、目录、图片、评论、收藏、登录等能力。 | 本项目 `engine/manga` adapters、`server/manga_api.py` 与客户端 JSON 契约；不把可执行 JS/QJS 插件运行时嵌入 APK，先以显式 adapter 能力/契约独立扩展。 |
| `foundation/local.dart` | 本地漫画实体与数据库管理；目录初始化/路径校验、作品搜索/排序、图片枚举、下载身份检查、下载任务恢复、批量删书/删章。 | Android `OfflineStore.kt`、Python 下载目录/清单扫描与漫画 API；借鉴“持久化章节清单+任务恢复”，但本地书架要求下载实盘有效图片校验、删除书架项不等于删媒体。 |
| `network/download.dart` | 下载任务抽象；图片任务按章节和图片并发调度、暂停/恢复/重试/限并发/速度统计及序列化恢复；另含归档下载任务。 | `engine/manga/{download_manager,downloader}.py`、Android 下载任务 UI；借鉴任务状态与续传模型，不照搬固定并发/重试，受源站限流、用户并发设置和完整性校验约束。 |
| `network/file_downloader.dart` | 大文件分块并发下载、保存分块状态、恢复未完成块、进度上报与取消。 | 当前章节图片下载器以单图请求为主，不需要直接移植归档分块器；若未来支持 CBZ/长包下载，应单独设计断点元数据与原子合并。 |
| `network/images.dart` | 封面/漫画图请求入口；将同 key 并发请求合并成共享流，转发字节进度、取消底层任务，并走源适配请求配置。 | `engine/manga` 图片获取、Web 图片加载、Android 本地引擎/Coil；采纳请求去重、取消和进度思想，维持本项目 source-specific timeout/backoff。 |
| `foundation/image_provider/reader_image.dart` | 阅读图片 provider：优先缓存，按源刷新图片 key/请求选项并下载字节，处理失败和可选图片缩放。 | 服务端图片 URL/代理与客户端图像加载；本项目用带章节身份的 URL/cache key，缓存命中不能伪装成正式下载，也不能跨 local/online 目录复用错误清单。 |
| `foundation/image_provider/local_comic_image.dart` | 从本地漫画路径读图片字节并参与 Flutter 图片解码。 | Android `OfflineStore.kt` + `catalog=local` 图片路由；要求本地入口不回源，路径必须受下载根约束且校验章节/页确实存在。 |
| `foundation/cache_manager.dart` | SQLite 缓存索引、命中续期、过期与空间上限清理、缓存统计；服务于可再生成的网络缓存。 | Python `_cache` 与 downloads 区分、Android/Web 图像缓存；本项目继续将临时缓存和用户明确下载分开统计、展示与删除。 |
| `foundation/history.dart` | SQLite 阅读历史模型/管理器；保存作品、章节 ep/group、页、最大页和 readEpisode，异步合并写入，批量刷新历史详情。 | `server/manga_api.py` 的阅读历史/章节 ID 集合、Android/Web 进度；借鉴异步写盘与详情刷新不覆盖阅读位置，身份采用 `(identity_source, comic_id, chapter_id)` 而不是可变序号。 |
| `foundation/favorites.dart` | 收藏夹数据库、文件夹、排序/移动/批量管理、收藏条目元数据和“读过”动作。 | 服务端 favorites API + Android/Web 书架；本项目关注单一作品收藏/自动收藏、源更新时间及跨源 identity，不照搬多文件夹业务。 |
| `foundation/follow_updates.dart` | 并发遍历收藏夹、刷新详情并按源更新时间识别更新、产生进度流与错误统计。 | 收藏启动更新检查和 `/check-updates` 后端任务；更新时间用于排序/展示，不作为未读计数本身，未读须按章节稳定身份差集及真实读史计算。 |

协作顺序可概括为：`loading.dart` 建会话 → `reader.dart` 管位置/模式 → `images.dart` 申请章节图片 → `reader_image.dart`/`images.dart` 请求与缓存 → `local_comic_image.dart` 处理严格本地图片 → `scaffold.dart`/`chapters.dart` 提供导航；`history.dart` 和 `favorites.dart` 分别持久化阅读身份与跟踪状态，`download.dart` / `local.dart` 负责把可恢复任务变成经验证的本地章节。这里是源码职责映射，不表示每一层都应按同名类一比一移植。

## 与当前项目实现对照

| 能力 | Venera 机制 | 本项目当前源码观察 | 采用判断 |
|---|---|---|---|
| 本地目录与混合阅读 | 一套 Reader 按每话已下载状态分支；书库内网络源未下载话也可能回源 | 引擎详情有 catalog=local 快路径；Android 书架阅读保留 localCatalog，本地图片 URL 强制带 catalog=local；在线详情走完整 readingChapters，已下载可本地优先 | 保留本项目显式双目录语义；避免书架误触在线源，并保留在线入口混合阅读。 |
| 卷/章结构 | ComicChapters 支持分组 map；全局章节序号按组插入顺序展开 | 引擎与客户端构造阅读目录时将 volumes 放在独立 chapters 之前，并保留卷内条目顺序 | 本项目的“卷在前、单章在后”规则更明确；继续用稳定章节 ID 而非标题推测。 |
| 阅读位置 | 1-based ep/page/group 加 readEpisode 集合 | Android/Web 保存 chapter_id + chapter_label + idx + pos，并对已读集合做章节身份归一；Android 页码 Slider 驱动当前内容滚动/翻页 | 继续以稳定章节身份为主，位置索引只作为兼容/恢复辅助；不要退回单纯序号模型。 |
| 下载状态 | SQLite 显式 downloadedChapters；任务完成后合并章节记录 | 本项目下载状态要求由实盘可读图片和章节身份共同证明，并兼容旧目录/源别名 | 本项目更抗陈旧数据库记录，但磁盘兼容复杂；保持“读得出才算已下载”，增加真实旧用户数据迁移验收。 |
| 图片请求 | JS 源按单图动态返回 headers/URL/处理回调；缓存层与 UI 集成 | Python 源适配器/引擎提供 URL、代理、缓存、下载；Android 通过本地服务与 Coil 加载，分可见图与低优先级预取通道 | 不移植 JS/QJS 插件运行时；把 Venera 的源能力矩阵变成 Python adapter 合同和契约测试。 |
| 预取 | 分页模式预载周边图片；连续模式预下载相邻页，章节切换时取消 | Android 已有独立慢速预取 loader、有界通道、尾部预取下一章、切章取消；Web 有章级预取代际防迟到覆盖 | 采用其“可见内容与邻接预取分离”的原则；本项目现有优先级/取消/代际保护更适合弱网，继续压测。 |
| 失败恢复 | 图片层自动最多 5 次重试，支持刷新源请求配置；UI 有人工重试 | Android 有章节 URL 重试和单页重试；Python 层有分阶段错误、下载恢复及章节重建 | 不直接照抄固定五次重试；按错误类型、退避、可取消性和源速率配置，避免风控时雪崩。 |
| 阅读模式/控制 | 多方向连续/分页、缩放、边缘切章、Slider、实体键/键盘事件等 | Android 有纵向连续/横向分页、缩放、小中心热区、进度 Slider；Web 已有方向键、翻页键和 Home/End 切章；Android 现已补齐方向键/翻页键及 Home/End 实体键导航 | 借鉴输入映射，不动触摸规则：翻页模式左右键翻页、连续模式上下键按视口步进、PageUp/PageDown 通用翻页、MoveHome/MoveEnd 切章节；书架/目录不接管阅读按键。 |
| 自动隐藏 | 控件初始隐藏；画布点击可切换；没有“显示一段时间后自动收起”的定时器 | Android `MangaScreens.kt` 与 Web `manga_reader.html` 均已默认隐藏，采用约 22%×14% 的小中心热区切换；显示后 4 秒空闲自动收起，拖动进度条/打开目录时暂停计时，滚动和热区外点击不误唤出 | 用户要求的阅读器专属显隐状态机已实现并有 Android AVD、隔离 Chrome 自动化证据；目标真机触摸手感和弱性能设备仍待验收。 |
| Web 进度控制 | Flutter 底栏 Slider 实时控制当前页 | `manga_reader.html` 已有实时页码、章节标识、前后页按钮及可拖动 Slider；单页/双页/连续模式分别映射到页位置或可见阅读位置，切章时取消过期预取 | Web 隔离 Chrome 内容态已验收滑杆与阅读位置联动；Android AVD 已实际拖动滑杆并断言跳到最后一页。真实源高抖动、目标手机手势体验仍待验收。 |
| 更新与未读 | 更新比较主要基于源 updateTime 字符串，收藏夹检查使用并发队列与节流 | 本项目目标是对收藏最新章节目录与稳定章节 ID 做差集，再依据实际读过的章节身份计算未读数 | 本项目的未读定义更强；不要移植 Venera 的“更新时间变化即有更新”作为未读算法。 |

## 不应直接复制的参考实现风险

1. HistoryManager 表中 id 是单列主键，但漫画身份还包含 type/sourceKey；若不同源出现相同作品 ID，SQLite INSERT OR REPLACE 有覆盖风险。本项目阅读身份应使用 (source, comic_id) 复合身份。
2. History 的已读章节集依赖 ep/group 序号；源目录插入、删减或重排后，序号可能指向另一章。本项目继续用章节 ID 作为读历史主键，并为目录变化做身份映射。
3. Venera 的书架网络作品可在未下载章节上回源，与本项目“书架严格本地”的产品要求相冲突。
4. ImageDownloader 返回缓存图片后仍可能继续执行网络加载；不能假设该接口始终是纯缓存命中短路。性能语义需独立压测，不按类名推断。
5. 固定重试次数、固定预载数量不适合所有源；CopyManga、JM、MangaDex 等源的防盗链、限流和图片规模不同，应由本项目适配层控制并发/退避。
6. Venera 的源插件回调可执行 JS 并对响应字节做后处理；把该运行时移入当前 APK 会新增脚本隔离、权限、签名、升级和故障治理风险，不属于必要的阅读器移植。

## 结论：移植/自制决策

- **独立自制**：阅读控制显隐状态机、Android 与 Web 的阅读进度条、稳定章节 ID 的目录/历史映射、书架本地目录约束、卷在前的规范目录、源级有界预取/退避、下载状态实盘验证。
- **只借鉴交互**：连续/翻页模式、源级阅读偏好、已下载标记、章节目录快速定位、异常页就地重试、横屏/双页与图片缩放。
- **按跨端差异补齐**：Web 已支持键盘阅读导航，Android 采用 Compose 原生按键事件自制同类辅助输入；本轮保留设备 AVD 交互验收，未引入 Venera 的 Flutter 手势实现。
- **不移植其代码/运行时**：Flutter 页面、Dart 数据层、JS 插件引擎、SQLite 表结构、图片下载器。它们和本项目 Python 引擎 + Android Compose + Web 模板的技术栈不同，且 GPL-3.0 有分发影响。

### 本轮差异与候选功能

- 已确认的跨端差异：Web 有单页/双页/连续模式；Android 当前为连续/横向分页。双页可作为 Android 平板/横屏的后续自制候选，但需先定义 RTL 装订方向、窄屏退化、旋转后进度映射和图片预取预算；现阶段不为“功能对齐”直接扩张 2.0.0 范围。
- 作品/书源级阅读偏好、图片收藏、CBZ/7z 导入、WebDAV、评论/评分均属于增量产品能力，不是修复当前阅读闭环所必需；分别需要偏好覆盖优先级、媒体删除/备份范围、文件安全、跨设备冲突处理或源能力协商设计。
- 用户明确指定的控制栏规则优先于上游交互：只在漫画阅读画布自动隐藏，窄小中心热区才切换显示；不得把整屏点击、长按或翻页点击复用成唤栏手势。

## 可执行的功能映射清单

以下映射按“用户可见能力”而不是 Venera 类名组织，便于后续每次只实现一个闭环。路径是本项目当前主要入口；同一能力若还有服务端或数据层参与，也一并列出。

| Venera 能力 | 本项目落点 | 状态/决策 | 下轮可验证的工作 |
|---|---|---|---|
| 章节阅读会话、页/章定位、历史保存 | `android/.../MangaScreens.kt`、`templates/manga_reader.html`、`server/manga_api.py` | 已自制稳定章节身份、页位置、续读和退出保存；不用 Venera 的序号型 History schema | 继续用目录插入/删章、来源别名、重建进程夹具跑跨端回归 |
| 连续滚动、单页翻页、图片缩放 | Android Compose 阅读屏；Web 阅读模板的 `setMode`、`renderPaged`、`zoomManga` | 两端均有连续/分页和缩放；Web 还支持双页模式，Android 目前是连续/横向分页二选一 | 决定双页是否适合手机与平板；若加入，先定义窄屏/横屏断点和阅读进度映射，再补 UI/设备测试 |
| 控件显隐与页进度 Slider | `isReaderControlHotZone` / 控件 idle-hide 状态；Web `isReaderCenterTap` / `scheduleReaderControlsHide` | 按用户要求只在阅读器中隐藏；中心热区小，滑杆拖动期间不自动收起；两端分别用原生实现 | 保持热区尺寸与阅读方向无关；设备验证边缘触摸、误触率、TalkBack/键盘可达性 |
| 目录分组、当前话定位、下载标记 | Compose LazyColumn 目录；Web `renderToc`；服务端规范化 `volumes + chapters` | 当前话打开即定位；统一卷优先；Web/Android 的目录实现不同但共享服务端顺序 | 对超长卷内子话、当前项跨卷、过滤后定位做视觉/仪器断言 |
| 图片可见加载、预取、失败重试 | Android Coil 可见/预取 loader；Web 图片渲染与 `_nextChPrefetch`；Python 下载/图片代理 | 按本项目源适配器、并发边界和取消语义自制；不照搬固定重试次数 | 源级超时/限速矩阵、断网恢复、切章取消、部分失败后重试实测 |
| 本地库与网络章节混合阅读 | `OfflineStore.kt`、`MangaScreens.kt`、`server/manga_api.py`、Web catalog 状态 | 有意比 Venera 更严格区分“仅本地目录”和“在线完整目录 + 本地优先”；该差异是产品契约而不是待抹平的端差 | 必须保留“本地入口零网络请求”及“在线入口逐章本地优先”的端到端测试 |
| 设备/作品/来源级阅读偏好 | Android `ReaderPrefs.kt` 全局 SharedPreferences；Web `manga_reader_settings` 单浏览器 localStorage | 当前持久化范围与 Venera 不同：没有统一的作品/源级覆盖策略；尚不判定为缺陷，避免无需求地增加设置复杂度 | 先收集实际需求；若要做，定义全局默认→来源覆盖→作品覆盖优先级及清除/迁移语义，Android/Web 各自存储但共用字段名/默认值 |
| 按键/键盘/外设导航 | Android `mangaReaderKeyAction`；Web 阅读模板键盘事件 | Android 的方向键按阅读模式解释，PageUp/Down 翻页、Home/End 切话；Web 有键盘导航 | 补一份共享按键契约测试表，实测蓝牙键盘/遥控器与焦点恢复，不把键盘映射耦合到目录列表 |
| 收藏更新与未读计数 | `server/manga_api.py`、`MainActivity.kt`、`library.js` | 采用章节身份差集和实际阅读集合；不采用 Venera 更新时间字符串充当未读算法 | 保留跳读/回读/新话/卷内多话的共享黄金样本，端上只展示服务端权威计数 |

### 行为契约（后续实现不可悄悄改变）

1. **严格本地入口**：目录和图片均只来自可验证的本机下载；源站不可用不能触发回源。
2. **在线混合入口**：完整源目录按“卷在前、独立章节在后”展示；每个章节本地可读则优先本地，缺失时才走在线源。
3. **进度身份**：source 的传输路由与跨端 identity 分离；历史以稳定章节 ID 为主，序号只作兼容/排序辅助。
4. **控制显隐**：仅漫画阅读画布响应小中心热区；书架、详情、目录滚动、进度拖动不能意外切换阅读控制状态。
5. **预取不抢前台**：切章/退出即取消旧预取；旧响应只有在章节身份和渲染代际均匹配时才可入缓存。
6. **下载状态**：缓存索引或服务器汇总数不能单独证明“已下载”；必须通过有效媒体、章节身份和本地目录映射验证。

这些契约是行为规格，不复用 Venera 的具体实现或其依赖；Android Compose、桌面 Web 和 Python 引擎按各自平台实现，并通过共享夹具保持结果一致。

### 2.0.0 后续闭环优先级

1. 在目标 Android 手机和真实作品上验收本地书架严格离线、在线目录混合阅读、部分下载后章节身份与下载状态刷新。
2. 继续补全桌面页面逐屏审视与跨端数据同一性，重点覆盖书库/收藏、详情、任务状态、刷新恢复和窄屏可达性。
3. 完成旧版覆盖升级、进程/旋转/低存储恢复与真实源的设备闭环；只有这些发布门槛通过后才构建 2.0.0 release。
4. 在决定接收任何 Venera 衍生代码前，单独确认许可证策略；默认保持独立实现。
