package com.webnovel.mobile

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * 离线本地索引（方向基线 §5.4 / §6.6）。
 *
 * 背景：主框架过去只在引擎 ready 时才出现，错误页却承诺"已下载内容仍可离线阅读"，
 * 而那时**根本进不去书架**。本文件让界面在**引擎完全不可用**时也能展示并使用
 * 已下载内容：直接读应用私有目录里的既有文件（Python 仍是业务写入权威，
 * 这里只做**只读**索引，不写任何业务文件，避免两套写入互相打架）。
 *
 * 目录契约（与引擎一致，改一处必须同时改另一处）：
 *   小说：`runtime/books/<key>/_state.json`（book/chapters/completed/failed）
 *         正文缓存 `runtime/books/<key>/<cache_key_of(url)>.cache`，内容为"章节名\n\n正文"
 *         阅读位置 `runtime/book_progress.json` → { "<key>": {"idx": N, "pct": …, "name": …, "ts": …} }
 *           ⚠ 服务端写的是 **`idx`**（见 server/novel_api.py::api_book_progress_save）。
 *           本文档曾写作 `index`，而读侧照着它 `optInt("index", 0)` → **永远读到 0**：
 *           离线书架显示不出"读到哪"、点开也总从第一章已下载章开始。两个键都读。
 *   漫画：`runtime/manga/_library.json`（书库元数据）
 *         已下载图片 `runtime/manga/downloads/<source>/<comic_id>/<chapter_id>/0000.jpg`
 */
object OfflineStore {

    fun runtimeDir(ctx: Context): File = File(ctx.filesDir, "runtime")

    /**
     * 章节 URL → 缓存文件名键。**必须与服务端 `engine/app_utils.cache_key_of`
     * 算出逐字相同的结果**（磁盘上的缓存文件名是服务端写的，键不同就找不到文件，
     * 离线阅读会误报"这一章没有下载到本机"）。
     *
     * 服务端实现是 `re.sub(r"[^\w\u4e00-\u9fff-]", "_", url)[-60:]`，其中 Python 的
     * `\w` 是 **Unicode 感知**的（等价于"Unicode 字母/数字 + 下划线"）。
     * 而 Kotlin/Java 的 `\w` 只匹配 ASCII，旧实现仅补了 `\u4e00-\u9fff`：
     * 假名（第1話 的 話 在范围内、但 ぁ-ん 不在）、韩文、CJK 扩展 A/B
     * 都会与服务端算出**不同的键**。这里改用 `\p{L}\p{N}_`（字母/数字/下划线），
     * 与服务端语义对齐。由 `OfflineIndexTest` 用真实数据钉住。
     */
    fun cacheKeyOf(url: String): String =
        Regex("[^\\p{L}\\p{N}_-]").replace(url, "_").takeLast(60)

    data class OfflineNovel(
        val key: String,
        val name: String,
        val author: String,
        val chapterCount: Int,
        val downloadedCount: Int,
        val lastIndex: Int,
        val lastChapterName: String,
        /** 已下载的章节序号（升序）：离线阅读只在这些章之间跳 */
        val downloadedIndices: List<Int> = emptyList(),
    ) {
        /** 打开时从哪一章开始：优先上次读到且已下载，否则第一章已下载 */
        val startIndex: Int
            get() = if (lastIndex in downloadedIndices) lastIndex
                    else (downloadedIndices.firstOrNull() ?: 1)

        val label: String
            get() {
                // ⚠ 必须先取到局部变量：`buildString { }` 的接收者是 StringBuilder，
                // 它带来的 `CharSequence.lastIndex`（= 当前长度-1）**遮蔽**本类的
                // `lastIndex` 字段。此前写成 `if (lastIndex in 1..chapterCount …)`
                // 实际比较的是"字符串长度-1 是否 ≤ 章数"，几乎恒为假
                // ——"读到…"这段**从来没有显示过**（2026-09-18 用 JVM 单测抓到）。
                val idx = lastIndex
                return buildString {
                    append(if (author.isBlank()) "佚名" else author)
                    append(" · 已下载 ").append(downloadedCount).append("/")
                        .append(chapterCount).append(" 章")
                    if (idx in 1..chapterCount && lastChapterName.isNotBlank()) {
                        append(" · 读到 ").append(lastChapterName)
                    }
                }
            }
    }

    data class OfflineManga(
        val source: String,
        val comicId: String,
        val title: String,
        val chapterCount: Int,
        val imageCount: Int,
    ) {
        val label: String get() = "已下载 $chapterCount 话 · $imageCount 张图"
    }

    private fun readJson(f: File): JSONObject? =
        runCatching { if (f.isFile) JSONObject(f.readText(Charsets.UTF_8)) else null }.getOrNull()

    private fun readJsonArray(f: File): JSONArray? =
        runCatching { if (f.isFile) JSONArray(f.readText(Charsets.UTF_8)) else null }.getOrNull()

    private fun readProgress(root: File): JSONObject? =
        readJson(File(root, "book_progress.json"))

    // ── 小说 ──

    fun novels(ctx: Context): List<OfflineNovel> = novelsFrom(runtimeDir(ctx))

    /**
     * 离线小说索引（**纯文件读取**，便于用 JVM 单测覆盖）。
     *
     * 抽成接收 root 而不是 Context 的原因：这段解析决定用户在离线模式下
     * "看到什么、从哪一章接着读"，历史上却只因为 `index`/`idx` 一个键名之差
     * 就永远读到 0（离线书架不显示"读到哪"、点开总回到第一章已下载章）。
     * 纯函数才好用用例钉住。
     */
    internal fun novelsFrom(root: File): List<OfflineNovel> {
        val booksDir = File(root, "books")
        if (!booksDir.isDirectory) return emptyList()
        val progress = readProgress(root)
        val out = ArrayList<OfflineNovel>()
        for (dir in booksDir.listFiles().orEmpty().sortedBy { it.name }) {
            if (!dir.isDirectory) continue
            val state = readJson(File(dir, "_state.json")) ?: continue
            val info = state.optJSONObject("book") ?: JSONObject()
            val chapters = state.optJSONArray("chapters") ?: JSONArray()
            val urls = (0 until chapters.length()).mapNotNull {
                chapters.optJSONObject(it)?.optString("url")?.takeIf { u -> u.isNotBlank() }
            }
            val downloadedIdx = urls.mapIndexedNotNull { i, u ->
                val f = File(dir, cacheKeyOf(u) + ".cache")
                if (f.isFile && f.length() > 0) i + 1 else null
            }
            if (downloadedIdx.isEmpty()) continue   // 一章正文都没有 → 离线不可读，不进离线书架
            var lastIndex = 0
            var lastNameRecord = ""
            runCatching {
                val p = progress?.optJSONObject(dir.name)
                if (p != null) {
                    // 当前契约是 `idx`；历史文档写 `index`。只认一个就会永远读到 0
                    // （离线书架显示不出"读到哪"、点开总从第一章开始）——两个都读。
                    lastIndex = if (p.has("idx")) p.optInt("idx", 0)
                                else p.optInt("index", 0)
                    lastNameRecord = p.optString("name").trim()
                }
            }
            // 章名优先用进度记录里的原话（它就是用户上次看到的那一章），
            // 记录里没有才回退到目录里对应序号的章名
            val lastName = lastNameRecord.ifBlank {
                if (lastIndex in 1..chapters.length()) {
                    chapters.optJSONObject(lastIndex - 1)?.optString("name") ?: ""
                } else ""
            }
            out.add(OfflineNovel(
                key = dir.name,
                name = info.optString("name").ifBlank { dir.name },
                author = info.optString("author"),
                chapterCount = chapters.length(),
                downloadedCount = downloadedIdx.size,
                lastIndex = lastIndex,
                lastChapterName = lastName,
                downloadedIndices = downloadedIdx,
            ))
        }
        return out
    }

    /** 离线读一章：返回正文（章节名 + 内容），没有缓存返回 null（界面照实说"这一章没下载"） */
    fun readNovelChapter(ctx: Context, key: String, index: Int): Pair<String, String>? =
        readNovelChapterFrom(runtimeDir(ctx), key, index)

    internal fun readNovelChapterFrom(root: File, key: String, index: Int): Pair<String, String>? {
        val dir = File(File(root, "books"), key)
        val state = readJson(File(dir, "_state.json")) ?: return null
        val chapters = state.optJSONArray("chapters") ?: return null
        if (index < 1 || index > chapters.length()) return null
        val entry = chapters.optJSONObject(index - 1) ?: return null
        val url = entry.optString("url")
        val name = entry.optString("name")
        val f = File(dir, cacheKeyOf(url) + ".cache")
        if (!f.isFile || f.length() == 0L) return null
        val raw = runCatching { f.readText(Charsets.UTF_8) }.getOrNull() ?: return null
        // 缓存文件格式："章节名\n\n正文"；按第一个空行切掉重复的标题
        val body = raw.split("\n\n", limit = 2).let { if (it.size == 2) it[1] else raw }
        return name to body
    }

    fun novelChapterCount(ctx: Context, key: String): Int =
        novelChapterCountFrom(runtimeDir(ctx), key)

    internal fun novelChapterCountFrom(root: File, key: String): Int {
        val state = readJson(File(File(File(root, "books"), key), "_state.json"))
            ?: return 0
        return state.optJSONArray("chapters")?.length() ?: 0
    }

    /** 已下载的章节序号（离线阅读器只在这些章之间跳，避免点进空白章） */
    fun novelDownloadedIndices(ctx: Context, key: String): List<Int> =
        novelDownloadedIndicesFrom(runtimeDir(ctx), key)

    internal fun novelDownloadedIndicesFrom(root: File, key: String): List<Int> {
        val dir = File(File(root, "books"), key)
        val state = readJson(File(dir, "_state.json")) ?: return emptyList()
        val chapters = state.optJSONArray("chapters") ?: return emptyList()
        val out = ArrayList<Int>()
        for (i in 0 until chapters.length()) {
            val url = chapters.optJSONObject(i)?.optString("url") ?: continue
            val f = File(dir, cacheKeyOf(url) + ".cache")
            if (f.isFile && f.length() > 0) out.add(i + 1)
        }
        return out
    }

    // ── 漫画 ──

    fun manga(ctx: Context): List<OfflineManga> = mangaFrom(runtimeDir(ctx))

    /** 离线漫画索引（**纯文件读取**，便于 JVM 单测覆盖；理由同 novelsFrom） */
    internal fun mangaFrom(rootDir: File): List<OfflineManga> {
        val mangaDir = File(rootDir, "manga")
        val root = File(mangaDir, "downloads")
        if (!root.isDirectory) return emptyList()
        val titles = HashMap<String, String>()          // "source|comicId" → title
        readJsonArray(File(mangaDir, "_library.json"))?.let { arr ->
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                val s = o.optString("source"); val c = o.optString("comic_id")
                if (s.isNotBlank() && c.isNotBlank()) {
                    titles["$s|$c"] = o.optString("title").ifBlank { c }
                }
            }
        }
        val out = ArrayList<OfflineManga>()
        for (src in root.listFiles().orEmpty().sortedBy { it.name }) {
            if (!src.isDirectory) continue
            for (comic in src.listFiles().orEmpty().sortedBy { it.name }) {
                if (!comic.isDirectory) continue
                var chapters = 0
                var images = 0
                for (ch in comic.listFiles().orEmpty()) {
                    if (!ch.isDirectory) continue
                    val n = ch.listFiles().orEmpty().count { isImage(it.name) }
                    if (n > 0) { chapters++; images += n }
                }
                if (chapters == 0) continue
                out.add(OfflineManga(
                    source = src.name, comicId = comic.name,
                    title = titles["${src.name}|${comic.name}"] ?: comic.name,
                    chapterCount = chapters, imageCount = images,
                ))
            }
        }
        return out.sortedByDescending { it.imageCount }
    }

    /** 已下载的（章节id → 图片文件列表，按页码排序） */
    fun mangaChapters(ctx: Context, source: String, comicId: String): List<Pair<String, List<File>>> {
        val dir = File(runtimeDir(ctx), "manga/downloads/$source/$comicId")
        if (!dir.isDirectory) return emptyList()
        val out = ArrayList<Pair<String, List<File>>>()
        for (ch in dir.listFiles().orEmpty().sortedBy { it.name }) {
            if (!ch.isDirectory) continue
            val imgs = ch.listFiles().orEmpty().filter { isImage(it.name) }
                .sortedBy { it.name }
            if (imgs.isNotEmpty()) out.add(ch.name to imgs)
        }
        return out
    }

    /**
     * 是否算"一张已下载的图"。
     *
     * ⚠ **必须与服务端的扩展名白名单逐字一致**：服务端在 5 处使用
     * `(".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif")`
     * （`server/manga_api.py` 的媒体路由与目录扫描、`engine/manga/downloader.py`
     * 的 `cache_path` 校验），且还有 AVIF magic byte 识别。
     * 客户端此前少了 `.avif` → 纯 avif 的章节会被**整章漏计**：图数偏少，
     * 若该话是唯一已下载的话，整部漫画甚至**不进离线书架**（内容明明在本机）。
     * 由 `OfflineIndexTest` 钉住这份契约。
     * 说明：`.avif` 的实际解码能力取决于系统（Android 12+ 的平台解码器支持），
     * 旧设备上与在线阅读同样可能不显示——这是解码能力问题，不是索引问题，
     * 索引必须先把"本机有什么"如实列出来。
     */
    internal fun isImage(name: String): Boolean {
        val n = name.lowercase()
        return n.endsWith(".jpg") || n.endsWith(".jpeg") || n.endsWith(".png") ||
            n.endsWith(".webp") || n.endsWith(".gif") || n.endsWith(".avif")
    }
}
