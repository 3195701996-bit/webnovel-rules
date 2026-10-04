package com.webnovel.mobile

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.Normalizer
import java.util.Locale
import java.util.concurrent.ConcurrentHashMap

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

    // Offline shelf and reader can request the same comic in one app session. Keep the
    // validated file index so opening its detail does not reopen every image header.
    // The fingerprint includes chapter-directory listings (not just the comic root,
    // whose mtime does not change when nested pages are added or removed).
    private data class MangaIndexEntry(
        val fingerprint: List<String>,
        val chapters: List<Pair<String, List<File>>>,
    )
    private val mangaIndex = ConcurrentHashMap<String, MangaIndexEntry>()
    private const val MANGA_INDEX_LIMIT = 32

    private fun mangaIndexKey(root: File, source: String, comicId: String) =
        "${root.absolutePath}|$source|$comicId"

    private fun mangaFingerprint(rootDir: File, source: String, comicId: String): List<String> =
        mangaSourceAliases(source).flatMap { alias ->
            listOf("downloads/$alias", "_cache/$alias").map { relative ->
                val comic = File(File(rootDir, "manga/$relative"), comicId)
                val children = comic.listFiles().orEmpty().filter { it.isDirectory }
                    .sortedBy { it.name }
                buildString {
                    append(comic.absolutePath).append(':')
                    append(comic.lastModified()).append(':').append(comic.exists())
                    for (chapter in children) {
                        append('|').append(chapter.name).append('@').append(chapter.lastModified())
                    }
                    val manifest = File(comic, "_info.json")
                    append("|manifest:").append(manifest.length()).append('@')
                        .append(manifest.lastModified())
                }
            }
        }

    private fun cachedMangaChapters(
        rootDir: File, source: String, comicId: String,
    ): List<Pair<String, List<File>>> {
        val key = mangaIndexKey(rootDir, source, comicId)
        val fingerprint = mangaFingerprint(rootDir, source, comicId)
        mangaIndex[key]?.takeIf { it.fingerprint == fingerprint }?.let { return it.chapters }
        val chapters = scanMangaChapters(rootDir, source, comicId)
        mangaIndex[key] = MangaIndexEntry(fingerprint, chapters)
        if (mangaIndex.size > MANGA_INDEX_LIMIT) {
            val oldest = mangaIndex.keys.firstOrNull { it != key }
            if (oldest != null) mangaIndex.remove(oldest)
        }
        return chapters
    }

    internal fun invalidateMangaIndex(root: File, source: String, comicId: String) {
        mangaSourceAliases(source).forEach { alias ->
            mangaIndex.remove(mangaIndexKey(root, alias, comicId))
        }
    }

    /** CopyManga APP/Web adapters share comic identities but older builds used separate roots. */
    internal fun mangaSourceAliases(source: String): List<String> =
        if (source == "copymanga" || source == "copymanga_web") {
            listOf("copymanga", "copymanga_web")
        } else listOf(source)

    internal fun canonicalMangaSource(source: String): String =
        if (source == "copymanga_web") "copymanga" else source

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
        val legacyCacheRoot = File(mangaDir, "_cache")
        if (!root.isDirectory && !legacyCacheRoot.isDirectory) return emptyList()
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
        val identities = LinkedHashSet<Pair<String, String>>()
        for (mediaRoot in listOf(root, legacyCacheRoot)) {
            for (src in mediaRoot.listFiles().orEmpty().sortedBy { it.name }) {
                if (!src.isDirectory) continue
                for (comic in src.listFiles().orEmpty().sortedBy { it.name }) {
                    if (!comic.isDirectory) continue
                    identities.add(canonicalMangaSource(src.name) to comic.name)
                }
            }
        }
        val out = identities.mapNotNull { (source, comicId) ->
            val chapters = cachedMangaChapters(rootDir, source, comicId)
            if (chapters.isEmpty()) return@mapNotNull null
            val title = mangaSourceAliases(source).asSequence()
                .mapNotNull { titles["$it|$comicId"]?.takeIf(String::isNotBlank) }
                .firstOrNull() ?: comicId
            OfflineManga(
                source = source,
                comicId = comicId,
                title = title,
                chapterCount = chapters.size,
                imageCount = chapters.sumOf { it.second.size },
            )
        }
        return out.sortedByDescending { it.imageCount }
    }

    /** 已下载的（章节id → 图片文件列表，按页码排序） */
    fun mangaChapters(ctx: Context, source: String, comicId: String): List<Pair<String, List<File>>> {
        return mangaChaptersFrom(runtimeDir(ctx), source, comicId)
    }

    internal fun mangaChaptersFrom(
        rootDir: File, source: String, comicId: String,
    ): List<Pair<String, List<File>>> = cachedMangaChapters(rootDir, source, comicId)

    private fun scanMangaChapters(
        rootDir: File, source: String, comicId: String,
    ): List<Pair<String, List<File>>> {
        data class MangaRoot(
            val alias: String,
            val dir: File,
            val metadata: Map<String, MangaChapterMetadata>,
            val legacyCache: Boolean,
        )
        data class Candidate(val root: MangaRoot, val pages: List<File>)

        val mangaDir = File(rootDir, "manga")
        val roots = mangaSourceAliases(source).flatMap { alias ->
            listOf(
                File(mangaDir, "downloads/$alias/$comicId") to false,
                File(mangaDir, "_cache/$alias/$comicId") to true,
            ).mapNotNull { (dir, legacyCache) ->
                if (!dir.isDirectory) return@mapNotNull null
                MangaRoot(alias, dir, chapterMetadata(dir), legacyCache)
            }
        }
        if (roots.isEmpty()) return emptyList()

        // Use one persisted catalog where available, but gather media from both legacy roots.
        val metadata = LinkedHashMap<String, MangaChapterMetadata>()
        roots.forEach { root -> root.metadata.forEach { (id, row) -> metadata.putIfAbsent(id, row) } }
        // Old CopyManga APP/Web adapters may persist different chapter IDs for the
        // same source chapter. Match their persisted Unicode-normalized labels, but
        // only when each alias has at most one row for that label; duplicate labels
        // within one catalog are ambiguous and must remain distinct.
        val idsByNameAndAlias = mutableMapOf<String, MutableMap<String, MutableSet<String>>>()
        roots.forEach { root ->
            root.metadata.forEach { (id, row) ->
                val name = mangaChapterIdentityName(row.sortName)
                if (name.isNotEmpty()) {
                    idsByNameAndAlias.getOrPut(name) { mutableMapOf() }
                        .getOrPut(root.alias) { mutableSetOf() }.add(id)
                }
            }
        }
        val ambiguousNames = idsByNameAndAlias.filterValues { byAlias ->
            byAlias.values.any { it.size > 1 }
        }.keys
        val ids = LinkedHashSet<String>()
        roots.forEach { root ->
            root.dir.listFiles().orEmpty().filter { it.isDirectory }
                .sortedBy { it.name }.forEach { ids.add(it.name) }
        }

        val out = ids.mapNotNull { chapterId ->
            val candidates = roots.mapNotNull { root ->
                // `_cache` also contains ordinary online reader cache. Only promote
                // chapters explicitly present in its legacy download manifest.
                if (root.legacyCache && chapterId !in root.metadata) return@mapNotNull null
                val dir = File(root.dir, chapterId).takeIf(File::isDirectory)
                    ?: return@mapNotNull null
                val pages = completeChapterPages(dir, root.metadata[chapterId]?.expectedPageCount)
                pages.takeIf(List<File>::isNotEmpty)?.let { Candidate(root, it) }
            }
            candidates.maxWithOrNull(
                compareBy<Candidate> { it.pages.size }
                    .thenBy { if (!it.root.legacyCache) 1 else 0 }
                    .thenBy { if (it.root.alias == canonicalMangaSource(source)) 1 else 0 },
            )?.let { chapterId to it.pages }
        }

        val canonicalIds = roots.filter {
            it.alias == canonicalMangaSource(source) && !it.legacyCache
        }.flatMap { it.metadata.keys }.toSet()
        val deduplicated = out.groupBy { (chapterId, _) ->
            val name = metadata[chapterId]?.let { mangaChapterIdentityName(it.sortName) }.orEmpty()
            if (name.isNotEmpty() && name !in ambiguousNames) "name:$name" else "id:$chapterId"
        }.values.mapNotNull { aliases ->
            val bestPages = aliases.maxByOrNull { it.second.size } ?: return@mapNotNull null
            val stableId = aliases.firstOrNull { it.first in canonicalIds }?.first
                ?: bestPages.first
            stableId to bestPages.second
        }

        return deduplicated.sortedWith(compareBy<Pair<String, List<File>>>(
            { metadata[it.first]?.let { row -> if (row.isVolume) 0 else 1 } ?: 1 },
            { metadata[it.first]?.sortCategory ?: Int.MAX_VALUE },
            { metadata[it.first]?.sortNumber ?: Double.POSITIVE_INFINITY },
            { metadata[it.first]?.sortVolume ?: Double.POSITIVE_INFINITY },
            { metadata[it.first]?.sortName ?: "" },
            { metadata[it.first]?.ordinal ?: Int.MAX_VALUE },
            { it.first },
        ))
    }

    private data class MangaChapterMetadata(
        val ordinal: Int,
        val isVolume: Boolean,
        val expectedPageCount: Int?,
        val sortCategory: Int,
        val sortNumber: Double,
        val sortVolume: Double,
        val sortName: String,
    )

    /** Keep alias matching aligned with the server's NFKC/whitespace/case-fold key. */
    private fun mangaChapterIdentityName(value: String): String =
        Normalizer.normalize(value, Normalizer.Form.NFKC)
            .trim().split(Regex("\\s+")).filter(String::isNotEmpty)
            .joinToString(" ").lowercase(Locale.ROOT)

    /** Use the persisted source catalog for both completeness checks and volume-first reading. */
    private fun chapterMetadata(comicDir: File): Map<String, MangaChapterMetadata> {
        val manifest = File(comicDir, "_info.json")
        if (!manifest.isFile) return emptyMap()
        return try {
            val rows = JSONObject(manifest.readText(Charsets.UTF_8)).optJSONArray("chapters")
                ?: return emptyMap()
            buildMap {
                for (i in 0 until rows.length()) {
                    val row = rows.optJSONObject(i) ?: continue
                    val id = row.optString("id")
                    if (id.isBlank()) continue
                    val name = row.optString("name")
                    val sort = mangaChapterSortKey(name)
                    val count = row.optInt("download_page_count", 0).takeIf { it > 0 }
                    put(id, MangaChapterMetadata(
                        ordinal = i,
                        isVolume = isVolumeOnlyTitle(name),
                        expectedPageCount = count,
                        sortCategory = sort.first,
                        sortNumber = sort.second,
                        sortVolume = sort.third,
                        sortName = name,
                    ))
                }
            }
        } catch (_: Exception) {
            emptyMap()
        }
    }

    /** Mirrors engine.manga.download_manager._VOL_ONLY_RE (whole-title match only). */
    private fun isVolumeOnlyTitle(title: String): Boolean {
        val number = "(?:\\d+|[一二三四五六七八九十百零〇廿卅]+)"
        val pattern = Regex(
            "(?:單行本|单行本)?\\s*(?:" +
                "第\\s*" + number + "\\s*[卷巻]|" +
                "[卷巻]\\s*" + number + "|" +
                number + "\\s*[卷巻]|" +
                "Vol(?:ume)?\\.?\\s*\\d+)\\s*",
            RegexOption.IGNORE_CASE,
        )
        return pattern.matches(title.trim())
    }

    /**
     * Keep legacy offline manifests in the same display order as the service's
     * `_sort_chapters`: numeric episodes first, then volume-only entries, then
     * appendices/unnumbered entries. New manifests may already be canonical, but
     * old users must not get a different chapter order merely because they are offline.
     */
    private fun mangaChapterSortKey(title: String): Triple<Int, Double, Double> {
        val name = title.trim()
        val numeral = "(?:\\d+(?:\\.\\d+)?|[零〇一二两兩三四五六七八九十百廿卅]+)"
        val episode = Regex("第\\s*($numeral)\\s*(?:话|話|回|章|集|话数|話数)")
            .find(name)?.groupValues?.getOrNull(1)?.let(::parseMangaNumber)
        val volume = Regex("(?:第\\s*($numeral)\\s*[卷巻]|Vol\\.?\\s*(\\d+(?:\\.\\d+)?)|[卷巻]\\s*($numeral))", RegexOption.IGNORE_CASE)
            .find(name)?.groupValues?.drop(1)?.firstOrNull { it.isNotEmpty() }
            ?.let(::parseMangaNumber) ?: 0.0
        if (episode != null) return Triple(0, episode, volume)

        Regex("^\\s*(\\d+(?:\\.\\d+)?)\\s*$").find(name)?.let {
            return Triple(0, it.groupValues[1].toDoubleOrNull() ?: Double.POSITIVE_INFINITY, 0.0)
        }
        val extra = Regex(
            "特别篇|特別篇|番外|休載|休载|贺图|賀圖|公告|通知|後記|后记|" +
                "外传|外傳|小剧场|小劇場|总集篇|總集篇|设定集|設定集|插图|插圖|" +
                "动画化|動畫化|纪念|紀念|预告|預告|附錄|附录|單本|单本|OVA|SP\\b",
            RegexOption.IGNORE_CASE,
        ).containsMatchIn(name)
        if (!extra) {
            Regex("(?<![\\d.])(\\d{1,4})\\s*[-–—－]\\s*(\\d+)(?=[\\s(（)）]|$)")
                .find(name)?.let {
                    val number = "${it.groupValues[1]}.${it.groupValues[2]}".toDoubleOrNull()
                    if (number != null) return Triple(0, number, 0.0)
                }
            Regex("(?<![\\d.\\-–—－~])(\\d{1,4}(?:\\.\\d+)?)(?=[\\s(（)）]|$)")
                .find(name)?.let {
                    val number = it.groupValues[1].toDoubleOrNull()
                    if (number != null) return Triple(0, number, 0.0)
                }
        }
        return Triple(1, Double.POSITIVE_INFINITY, if (isVolumeOnlyTitle(name)) volume else Double.POSITIVE_INFINITY)
    }

    private fun parseMangaNumber(token: String): Double {
        token.toDoubleOrNull()?.let { return it }
        val digits = mapOf('零' to 0, '〇' to 0, '一' to 1, '二' to 2, '两' to 2,
            '兩' to 2, '三' to 3, '四' to 4, '五' to 5, '六' to 6, '七' to 7,
            '八' to 8, '九' to 9)
        if (token == "廿") return 20.0
        if (token == "卅") return 30.0
        if (token.startsWith("廿")) return 20.0 + parseMangaNumber(token.drop(1))
        if (token.startsWith("卅")) return 30.0 + parseMangaNumber(token.drop(1))
        if ('十' !in token && '百' !in token) {
            return token.mapIndexed { index, char ->
                (digits[char] ?: return Double.POSITIVE_INFINITY) *
                    Math.pow(10.0, (token.length - index - 1).toDouble())
            }.sum()
        }
        var section = 0
        var pending: Int? = null
        for (char in token) {
            when {
                char in digits -> pending = digits.getValue(char)
                char == '十' -> { section += (pending ?: 1) * 10; pending = null }
                char == '百' -> { section += (pending ?: 1) * 100; pending = null }
                else -> return Double.POSITIVE_INFINITY
            }
        }
        return (section + (pending ?: 0)).toDouble()
    }

    /** New manifests require the same complete, zero-based sequence as server.state. */
    private fun completeChapterPages(chapterDir: File, expectedCount: Int?): List<File> {
        val pages = sortedMapOf<Int, File>()
        for (file in chapterDir.listFiles().orEmpty().sortedBy { it.name }) {
            val index = pageIndex(file.name) ?: continue
            if (isReadableImage(file) && index !in pages) pages[index] = file
        }
        if (pages.isEmpty()) return emptyList()
        // Even legacy manifests without an authoritative page count must reject
        // provable interior gaps. Otherwise a partial download such as 0000, 0002
        // is incorrectly exposed as a complete offline chapter.
        if (pages.keys.withIndex().any { (position, page) -> page != position }) {
            return emptyList()
        }
        if (expectedCount != null && pages.size != expectedCount) return emptyList()
        return pages.values.toList()
    }

    internal fun pageIndex(name: String): Int? {
        val match = Regex("^(\\d+)(?:\\.[^.]+)?$").matchEntire(name) ?: return null
        return match.groupValues[1].toIntOrNull()
    }

    /** Align offline discovery with the engine's image-header integrity check. */
    internal fun isReadableImage(file: File): Boolean {
        if (!isImage(file.name) || pageIndex(file.name) == null || !file.isFile) return false
        return try {
            val header = ByteArray(12)
            java.io.DataInputStream(file.inputStream()).use { it.readFully(header) }
            (header[0] == 0xff.toByte() && header[1] == 0xd8.toByte()) ||
                header.copyOfRange(0, 8).contentEquals(
                    byteArrayOf(0x89.toByte(), 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a)) ||
                header.copyOfRange(0, 6).contentEquals("GIF87a".toByteArray()) ||
                header.copyOfRange(0, 6).contentEquals("GIF89a".toByteArray()) ||
                (header.copyOfRange(0, 4).contentEquals("RIFF".toByteArray()) &&
                    header.copyOfRange(8, 12).contentEquals("WEBP".toByteArray())) ||
                (header.copyOfRange(4, 8).contentEquals("ftyp".toByteArray()) &&
                    (header.copyOfRange(8, 12).contentEquals("avif".toByteArray()) ||
                        header.copyOfRange(8, 12).contentEquals("avis".toByteArray())))
        } catch (_: Exception) {
            false
        }
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
