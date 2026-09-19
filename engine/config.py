"""全局配置：超时/并发/缓存 TTL 等常量统一管理（消除魔法数字）"""

# ── 目录与环境 ──
# 说明：R16 的 web/android 平台分层（按 sys.platform 猜平台）已经移除，
# 现在由**显式注入**决定运行形态：
#   - 桌面/服务器：不设 WR_DATA_DIR，用仓库内 data/，浏览器访问同一份数据；
#   - Android App：server/mobile_entry.py 在导入本模块前注入 WR_DATA_DIR /
#     WR_CACHE_DIR / WR_SOURCES_DIR / WR_PROFILE=mobile，服务只监听 127.0.0.1；
#   - 测试与工具：用同样的变量隔离数据目录。
# 因此这里只认环境变量，不做平台嗅探（避免"看起来在桌面跑、实际按手机配置"）。
import os
HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OVERRIDE = os.environ.get("WR_DATA_DIR")
if _OVERRIDE:
    DATA_DIR = _OVERRIDE
else:
    DATA_DIR = os.path.join(HUB_DIR, "data")
BOOKS_DIR = os.path.join(DATA_DIR, "books")
TASKS_DIR = os.path.join(DATA_DIR, "tasks")
# 书源目录：WR_SOURCES_DIR 显式指定时优先，否则用仓库内 sources/
_SOURCES_OVERRIDE = os.environ.get("WR_SOURCES_DIR", "").strip()
SOURCES_DIR = _SOURCES_OVERRIDE or os.path.join(HUB_DIR, "sources")
CACHE_FILE_DIR = DATA_DIR

# ── 运行 profile（显式声明，不做平台自动探测）──
# 设计 §9：手机端需要独立资源预算。这里用**显式环境变量** WR_PROFILE=mobile|desktop
# 选择配置档，不做 sys.platform 之类的隐式判断——避免"看起来在桌面跑、实际按手机
# 配置"这类难以排查的行为差异。默认 desktop，行为与既有完全一致。
PROFILE = (os.environ.get("WR_PROFILE") or "desktop").strip().lower()
IS_MOBILE = PROFILE == "mobile"

# ── 任务状态 ──
ST_RUNNING = "running"
ST_PAUSED = "paused"
ST_STOPPED = "stopped"
ST_ERROR = "error"
ST_DONE = "done"
RESTARTABLE = (ST_PAUSED, ST_STOPPED, ST_ERROR)

# ── 搜索并发（按 profile）──
if IS_MOBILE:
    SEARCH_MAX_WORKERS = 8          # 设计 §9：手机 8（桌面 64）
    SEARCH_SOURCE_TIMEOUT = 9       # 移动网络更慢，单源时限放宽
else:
    SEARCH_MAX_WORKERS = 64
    SEARCH_SOURCE_TIMEOUT = 6
# P1-3: 死配置 SEARCH_WAIT_SECONDS/SEARCH_EXTRA_WAIT 已删除（全仓零引用，
# 单源时限由 SEARCH_SOURCE_TIMEOUT + 业务 deadline 熔断承担）
SLOW_COOLDOWN = 90             # 慢源冷却秒数
SEARCH_CACHE_TTL = 3600        # 搜索缓存 1h
TOC_CACHE_TTL = 6 * 3600       # toc 缓存 6h
ENHANCE_MAX_WORKERS = 2 if IS_MOBILE else 30   # 详情增强并发（手机 2）
ENHANCE_WAIT_SECONDS = 8       # 详情增强总时限
STREAM_STOP_EXACT = 5          # 精确命中阈值（提前停止）

# ── 爬取并发（按 profile）──
CRAWL_DEFAULT_CONCURRENCY = 2 if IS_MOBILE else 8
CRAWL_MAX_CONCURRENCY = 4 if IS_MOBILE else 16
# ── 正文章节取数预算（2026-09-18 实测缺陷）────────────────────────────
# 分页章节（nextContentUrl）是**顺序**取页的，整章耗时 = 页数 × 单页耗时。
# 实测：精华书阁《剑来》第301章有 **13+ 页、每页 ~1.9s**，25 秒预算在第 13 页耗尽，
# 循环 `break` → **静默返回断在句子中间的残章**（原文结尾"…若是前"），
# 下载与在线阅读都会永久读到半章。
# 代码里 P2-5 早就定过规矩"分页中途失败 = 整章失败，不能静默截断"，
# 但"预算耗尽"这条路径当时漏了。现在：预算按"整章"给足（默认 90 秒），
# 预算/页数用尽而仍有下一页时**显式抛错**（走失败重试或如实标注），绝不静默截断。
CONTENT_DEADLINE = 90          # 整章预算（秒），调用方可用 deadline 覆盖
CONTENT_MAX_PAGES = 40         # 正文分页上限（撞上限仍有下一页 → 显式报错，绝不截断）
# 目录分页上限（2026-09-18 实测缺陷）：精华书阁《剑来》目录 **66 页 / 1309 章**，
# 而 jhsssd 适配器写死 `range(30)` → 只取到 605 章（46%），
# 用户从该源下载的书**后半本读不到**。目录页数比正文多得多，上限必须给足。
TOC_PAGE_CAP = 120             # 目录分页上限（撞上限仍有下一页 → 显式报错）
CRAWL_RETRY_MAX = 3            # 章节失败最大重试次数
CRAWL_RETRY_DELAY = 1.0        # 失败重排延迟秒
# 每域名并发上限：web 提到 8（原 4 会把整书爬取卡在 4 并发，实际还有按域名
# 限流 6/s + Fetcher 自适应降速兜底，触发限流才会等待，不会更猛）
FETCH_HOST_CONCURRENCY = 2 if IS_MOBILE else 8    # 每域名并发（设计 §9：手机 2）
# P1-7: 小说域 Fetcher 每域名 curl_cffi Session 池大小。
# curl_cffi Session 非线程安全：池内每个成员配独立锁，借出独占、归还释放，
# 避免高并发共享单 Session 造成的连接错乱（对齐漫画域 downloader.py 图片
# Session 池模式）。池 < 每域名并发信号量（FETCH_HOST_CONCURRENCY=8）是
# 正常的——多余并发线程在池成员锁上等待即自然排队，同时保留跨请求
# keep-alive 连接复用。
FETCH_SESSION_POOL_SIZE = 3
RATE_LIMIT_N = 6               # 每域名限流：窗口内次数
RATE_LIMIT_MS = 1000           # 每域名限流：窗口毫秒
# 目录 nextTocUrl 链式翻页的固定间隔（秒）。默认 0 = 关闭固定 sleep：
# Fetcher 每域名滑动窗口限流（RATE_LIMIT_N/RATE_LIMIT_MS + AIMD 自适应降速）
# 已兜底，正文分页路径本就不睡（crawler.get_content），目录链式翻页再固定
# sleep(0.6~1.3s) 属双重节流——50 页链式目录额外多花 ~50s。
# 个别对请求间隔极端敏感的源可调回 0.1~0.5 恢复固定间隔（代价是爬取变慢）。
TOC_PAGE_SLEEP = 0

# ── 缓存文件 ──
TOC_CACHE_FILE = os.path.join(DATA_DIR, "toc_cache.json")
SEARCH_CACHE_FILE = os.path.join(DATA_DIR, "search_cache.json")
# 全局阅读进度（book_key → {idx, pct, name, ts}）
BOOK_PROGRESS_FILE = os.path.join(DATA_DIR, "book_progress.json")

# ── 校验 ──
VALIDATE_TIMEOUT = 25
VALIDATE_FAST_TIMEOUT = 8
CHECK_KEYWORDS = ["剑来", "斗破苍穹", "赘婿", "诡秘之主", "凡人修仙传"]

# ── 漫画 ──
MANGA_DIR = os.path.join(DATA_DIR, "manga")
MANGA_LIBRARY_FILE = os.path.join(MANGA_DIR, "_library.json")
MANGA_HISTORY_FILE = os.path.join(MANGA_DIR, "_history.json")
MANGA_FAV_FILE = os.path.join(MANGA_DIR, "_favorites.json")
MANGA_TASKS_FILE = os.path.join(MANGA_DIR, "_tasks.json")
MANGA_CACHE_DIR = os.path.join(MANGA_DIR, "_cache")
MANGA_DOWNLOADS_DIR = os.path.join(MANGA_DIR, "downloads")
MANGA_STATE_DIR = os.path.join(MANGA_DIR, "_state")
MANGA_DETAIL_CACHE_TTL = 7 * 86400   # 详情缓存 7 天（避免重复触发风控）
# 漫画搜索单页条数：适配器 offset 计算与前端翻页判定的唯一事实源
# （此前 30 硬编码散落在适配器与模板共 4 处）
MANGA_PAGE_SIZE = 30
