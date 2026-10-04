package com.webnovel.mobile

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * 离线索引（引擎不可用时的书架与阅读落点）。
 *
 * 真实缺陷（2026-09-18 定位）：服务端把阅读位置写成 `{"idx":N,"pct":…,"name":…,"ts":…}`
 * （`server/novel_api.py::api_book_progress_save`），而 OfflineStore 按文档契约读的是
 * `optInt("index", 0)` —— **永远读到 0**。后果：离线书架从不显示"读到哪"，
 * 点开一本已下载的书**总回到第一章已下载章**，而不是用户上次读到的地方。
 * 这是"离线也能读已下载内容"这条承诺的核心体验，因此用用例钉住两个键都读。
 */
class OfflineIndexTest {

    private fun ev(line: String) = println("OFFLINE_INDEX_EVIDENCE $line")

    private fun tmpRoot(): File =
        Files.createTempDirectory("wr-offline-test").toFile()

    /** 造一本书：`chapters` 章，前 `downloaded` 章有缓存文件 */
    private fun makeBook(root: File, key: String, chapters: Int, downloaded: Int) {
        val dir = File(root, "books/$key")
        dir.mkdirs()
        val arr = JSONArray()
        for (i in 1..chapters) {
            val url = "https://t.test/read/1/$i.html"
            arr.put(JSONObject().put("name", "第${i}章").put("url", url))
            if (i <= downloaded) {
                File(dir, OfflineStore.cacheKeyOf(url) + ".cache")
                    .writeText("第${i}章\n\n正文。", Charsets.UTF_8)
            }
        }
        File(dir, "_state.json").writeText(
            JSONObject().put("book", JSONObject().put("name", key).put("author", "作者"))
                .put("chapters", arr).toString(), Charsets.UTF_8)
    }

    private fun writeProgress(root: File, key: String, json: JSONObject) {
        File(root, "book_progress.json").writeText(
            JSONObject().put(key, json).toString(), Charsets.UTF_8)
    }

    @Test
    fun currentServerSchemaIdxIsRead() {
        val root = tmpRoot()
        makeBook(root, "book_a", chapters = 10, downloaded = 5)
        writeProgress(root, "book_a", JSONObject()
            .put("idx", 3).put("pct", 40).put("name", "第3章 惊蛰").put("ts", 1.0))
        val n = OfflineStore.novelsFrom(root).single()
        assertEquals("服务端写的是 idx，必须读到", 3, n.lastIndex)
        assertEquals(3, n.startIndex)
        println("DEBUG lastIndex=${n.lastIndex} lastChapterName=${n.lastChapterName} label=${n.label}")
        assertTrue("离线书架要显示读到哪：${n.label}", n.label.contains("第3章 惊蛰"))
        ev("idx 契约：lastIndex=${n.lastIndex} start=${n.startIndex} label=${n.label}")
    }

    @Test
    fun legacyIndexKeyStillWorks() {
        val root = tmpRoot()
        makeBook(root, "book_b", chapters = 10, downloaded = 5)
        writeProgress(root, "book_b", JSONObject().put("index", 4))
        val n = OfflineStore.novelsFrom(root).single()
        assertEquals("老契约 index 也要读（兼容旧文档/旧数据）", 4, n.lastIndex)
        assertEquals(4, n.startIndex)
    }

    @Test
    fun withoutProgressStartsAtFirstDownloaded() {
        val root = tmpRoot()
        makeBook(root, "book_c", chapters = 10, downloaded = 0)
        // 只下第 4~6 章：startIndex 必须是第一个已下载章（而不是 1，那章根本没缓存）
        val dir = File(root, "books/book_c")
        for (i in 4..6) {
            File(dir, OfflineStore.cacheKeyOf("https://t.test/read/1/$i.html") + ".cache")
                .writeText("第${i}章\n\n正文。", Charsets.UTF_8)
        }
        val n = OfflineStore.novelsFrom(root).single()
        assertEquals(0, n.lastIndex)
        assertEquals(4, n.startIndex)
        assertFalse("没读过就不该编出'读到'文案", n.label.contains("读到"))
        ev("无进度：start=${n.startIndex} label=${n.label}")
    }

    @Test
    fun progressOnUndownloadedChapterStillShowsWhereAndOpensReadable() {
        val root = tmpRoot()
        makeBook(root, "book_d", chapters = 10, downloaded = 2)   // 只下了 1~2 章
        writeProgress(root, "book_d", JSONObject().put("idx", 8).put("name", "第8章"))
        val n = OfflineStore.novelsFrom(root).single()
        assertEquals(8, n.lastIndex)
        assertTrue("读到哪要照实显示：${n.label}", n.label.contains("第8章"))
        assertEquals("该章没下载 → 落点回退到第一个已下载章", 1, n.startIndex)
        ev("进度在第8章但未下载：start=${n.startIndex}")
    }

    @Test
    fun outOfRangeProgressDoesNotBreakTheIndex() {
        val root = tmpRoot()
        makeBook(root, "book_e", chapters = 3, downloaded = 3)
        writeProgress(root, "book_e", JSONObject().put("idx", 999).put("name", "第999章"))
        val n = OfflineStore.novelsFrom(root).single()
        // 超出目录范围：不参与 startIndex，也不显示为章名
        assertEquals(1, n.startIndex)
        assertFalse("越界记录不该当成章名展示", n.label.contains("第999章"))
        ev("越界进度：start=${n.startIndex} label=${n.label}")
    }

    @Test
    fun oneChapterWithoutCacheIsExcludedFromOfflineShelf() {
        val root = tmpRoot()
        makeBook(root, "book_f", chapters = 5, downloaded = 0)   // 一章正文都没有
        assertTrue("一章都没下载 → 不该出现在离线书架",
            OfflineStore.novelsFrom(root).isEmpty())
    }

    // ── 图片扩展名契约（与服务端白名单逐字一致）────────────────────────
    /**
     * 服务端使用的白名单（5 处，见 `server/manga_api.py` 媒体路由与目录扫描、
     * `engine/manga/downloader.py::cache_path`）：
     *   ".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif"
     * 客户端少一个 `.avif` 的后果是**整章漏计**（图数偏少；若是唯一已下载的话，
     * 整部漫画不进离线书架——内容明明在本机）。
     */
    @Test
    fun imageExtensionsMatchServerWhitelist() {
        val serverWhitelist = listOf("webp", "jpg", "jpeg", "png", "gif", "avif")
        for (ext in serverWhitelist) {
            assertTrue("服务端允许 .$ext，客户端必须也认：0001.$ext",
                OfflineStore.isImage("0001.$ext"))
            assertTrue("大写扩展名也要认：0001.${ext.uppercase()}",
                OfflineStore.isImage("0001.${ext.uppercase()}"))
        }
        // 非图片：不能把 JSON/临时文件/未知后缀算成图
        for (name in listOf("_info.json", "0001.jpg.tmp", "0001.webp2", "0001", "cover")) {
            assertFalse("不该算成图片：$name", OfflineStore.isImage(name))
        }
        ev("扩展名契约：服务端 ${serverWhitelist.size} 种全部识别，非图片全部排除")
    }

    @Test
    fun avifOnlyChapterIsCountedNotSkipped() {
        val root = tmpRoot()
        val dir = File(root, "manga/downloads/copymanga/comic_avif/4bd05882-ch")
        dir.mkdirs()
        val avifHeader = byteArrayOf(0, 0, 0, 0) + "ftypavif".toByteArray()
        for (i in 0..2) File(dir, String.format("%04d.avif", i))
            .writeBytes(avifHeader + ByteArray(32))
        val lib = JSONArray().put(JSONObject().put("source", "copymanga")
            .put("comic_id", "comic_avif").put("title", "全 AVIF 的一话"))
        File(root, "manga/_library.json").writeText(lib.toString(), Charsets.UTF_8)
        val list = OfflineStore.mangaFrom(root)
        assertEquals("纯 avif 章节必须被统计，否则整部漫画会从离线书架消失", 1, list.size)
        assertEquals(3, list.single().imageCount)
        assertEquals("全 AVIF 的一话", list.single().title)
        ev("纯 avif 章节：${list.single().label}")
    }

    @Test
    fun duplicateImageFormatsForSamePageCountAsOneReadablePage() {
        val root = tmpRoot()
        val chapter = File(root,
            "manga/downloads/copymanga/duplicate_format/chapter-1").apply { mkdirs() }
        val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte()) + ByteArray(32)
        val webp = "RIFF".toByteArray() + ByteArray(4) + "WEBP".toByteArray() + ByteArray(32)
        File(chapter, "0000.jpg").writeBytes(jpeg)
        File(chapter, "0000.webp").writeBytes(webp)
        File(chapter, "0001.webp").writeBytes(webp)

        val shelf = OfflineStore.mangaFrom(root)
        assertEquals("磁盘双格式副本不应虚增 APK 可阅读页数", 2,
            shelf.single().imageCount)
        assertEquals("阅读器也必须按唯一页码选图", 2,
            OfflineStore.mangaChaptersFrom(root, "copymanga", "duplicate_format")
                .single().second.size)
        ev("相同页码 JPG/WebP 双格式：书架与阅读目录均按唯一页码计")
    }

    @Test
    fun corruptImageExtensionDoesNotCreateOfflineDownloadedComic() {
        val root = tmpRoot()
        val chapter = File(root,
            "manga/downloads/copymanga/html_error/chapter-1").apply { mkdirs() }
        File(chapter, "0000.jpg").writeText("<html>403 forbidden</html>")
        assertTrue(
            "风控 HTML 即使被命名为 jpg，也不能让 APK 与服务端产生相反的已下载判断",
            OfflineStore.mangaFrom(root).isEmpty(),
        )
    }

    @Test
    fun offlineMangaIndexIsReusedAndInvalidatedWhenChapterDirectoryChanges() {
        val root = tmpRoot()
        val comic = File(root, "manga/downloads/jm/index-cache")
        val chapter = File(comic, "chapter-1").apply { mkdirs() }
        val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte()) + ByteArray(32)
        File(chapter, "0000.jpg").writeBytes(jpeg)

        val first = OfflineStore.mangaChaptersFrom(root, "jm", "index-cache")
        val repeated = OfflineStore.mangaChaptersFrom(root, "jm", "index-cache")
        assertTrue("同一份实盘索引应复用，避免重复读取每张图片头", first === repeated)

        val nextChapter = File(comic, "chapter-2").apply { mkdirs() }
        File(nextChapter, "0000.jpg").writeBytes(jpeg)
        assertTrue("章节目录新增后必须立即重建索引",
            nextChapter.setLastModified(System.currentTimeMillis() + 2_000))
        assertEquals(listOf("chapter-1", "chapter-2"),
            OfflineStore.mangaChaptersFrom(root, "jm", "index-cache").map { it.first })

        OfflineStore.invalidateMangaIndex(root, "jm", "index-cache")
        assertEquals(2, OfflineStore.mangaChaptersFrom(root, "jm", "index-cache").size)
    }

    @Test
    fun copyMangaLegacyAliasesMergeIntoOneOfflineIdentityAndDeduplicatePages() {
        val root = tmpRoot()
        val comicId = "legacy-copy-id"
        val canonicalDir = File(root, "manga/downloads/copymanga/$comicId")
        val webDir = File(root, "manga/downloads/copymanga_web/$comicId")
        val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte(), 0xe0.toByte()) +
            ByteArray(16)

        // Same chapter exists in both legacy roots. The canonical copy is partial;
        // the web-adapter copy is complete and should be the local reader's source.
        File(canonicalDir, "shared").apply { mkdirs() }.resolve("0000.jpg").writeBytes(jpeg)
        File(webDir, "shared").apply { mkdirs() }.resolve("0000.jpg").writeBytes(jpeg)
        File(webDir, "shared").resolve("0001.jpg").writeBytes(jpeg)
        File(canonicalDir, "volume-1").apply { mkdirs() }.resolve("0000.jpg").writeBytes(jpeg)
        File(webDir, "episode-2").apply { mkdirs() }.resolve("0000.jpg").writeBytes(jpeg)

        File(canonicalDir, "_info.json").writeText(
            JSONObject().put("chapters", JSONArray()
                .put(JSONObject().put("id", "shared").put("name", "第1话"))
                .put(JSONObject().put("id", "volume-1").put("name", "第一卷")))
                .toString(), Charsets.UTF_8,
        )
        File(webDir, "_info.json").writeText(
            JSONObject().put("chapters", JSONArray()
                .put(JSONObject().put("id", "shared").put("name", "第1话"))
                .put(JSONObject().put("id", "episode-2").put("name", "第2話")))
                .toString(), Charsets.UTF_8,
        )
        File(root, "manga/_library.json").apply {
            parentFile?.mkdirs()
            writeText(JSONArray().put(JSONObject().put("source", "copymanga_web")
                .put("comic_id", comicId).put("title", "历史通道下载的作品")).toString(), Charsets.UTF_8)
        }

        val shelf = OfflineStore.mangaFrom(root)
        assertEquals("别名目录必须合并成一部作品", 1, shelf.size)
        assertEquals("离线作品使用稳定的规范 source 身份", "copymanga", shelf.single().source)
        assertEquals(comicId, shelf.single().comicId)
        assertEquals("书库中保存的历史通道标题应保留", "历史通道下载的作品", shelf.single().title)
        assertEquals("共享章节只能计数一次，且选择完整可读副本", 3, shelf.single().chapterCount)
        assertEquals(4, shelf.single().imageCount)

        val expectedOrder = listOf("volume-1", "shared", "episode-2")
        val canonicalReader = OfflineStore.mangaChaptersFrom(root, "copymanga", comicId)
        val aliasReader = OfflineStore.mangaChaptersFrom(root, "copymanga_web", comicId)
        assertEquals("卷必须排在章节前，目录要合并两个历史根", expectedOrder,
            canonicalReader.map { it.first })
        assertEquals("两个 source 入口必须解析到相同的离线章节身份", expectedOrder,
            aliasReader.map { it.first })
        assertEquals("重复章节应选用完整的两页本地副本", 2,
            canonicalReader.first { it.first == "shared" }.second.size)
        ev("CopyManga 别名：合并为 1 部，下载单元 3 话/4 图，共享 chapter 去重并保持卷优先")
    }

    @Test
    fun copyMangaLegacyAliasesDeduplicateUnicodeEquivalentDifferentChapterIds() {
        val root = tmpRoot()
        val comicId = "legacy-copy-unicode-id"
        val appDir = File(root, "manga/downloads/copymanga/$comicId")
        val webDir = File(root, "manga/downloads/copymanga_web/$comicId")
        val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte(), 0xe0.toByte()) +
            ByteArray(16)
        File(appDir, "app-chapter-id").apply { mkdirs() }
            .resolve("0000.jpg").writeBytes(jpeg)
        File(webDir, "web-chapter-id").apply { mkdirs() }
            .resolve("0000.jpg").writeBytes(jpeg)
        File(webDir, "web-chapter-id").resolve("0001.jpg").writeBytes(jpeg)
        File(appDir, "_info.json").writeText(
            JSONObject().put("chapters", JSONArray().put(
                JSONObject().put("id", "app-chapter-id").put("name", "第1卷"),
            )).toString(), Charsets.UTF_8,
        )
        File(webDir, "_info.json").writeText(
            JSONObject().put("chapters", JSONArray().put(
                JSONObject().put("id", "web-chapter-id").put("name", "第１卷"),
            )).toString(), Charsets.UTF_8,
        )

        val chapters = OfflineStore.mangaChaptersFrom(root, "copymanga", comicId)
        assertEquals("不同 APP/Web ID 的全角/半角同卷只出现一次",
            listOf("app-chapter-id"), chapters.map { it.first })
        assertEquals("选择页数更多且可读的别名副本", 2, chapters.single().second.size)
    }

    @Test
    fun copyMangaLegacyAliasesDoNotMergeDuplicateTitlesWithinOneCatalog() {
        val root = tmpRoot()
        val comicId = "legacy-copy-ambiguous-id"
        val appDir = File(root, "manga/downloads/copymanga/$comicId")
        val webDir = File(root, "manga/downloads/copymanga_web/$comicId")
        val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte(), 0xe0.toByte()) +
            ByteArray(16)
        listOf("episode-a", "episode-b").forEach { id ->
            File(appDir, id).apply { mkdirs() }.resolve("0000.jpg").writeBytes(jpeg)
        }
        File(webDir, "episode-web").apply { mkdirs() }.resolve("0000.jpg").writeBytes(jpeg)
        File(appDir, "_info.json").writeText(
            JSONObject().put("chapters", JSONArray().apply {
                put(JSONObject().put("id", "episode-a").put("name", "第１话"))
                put(JSONObject().put("id", "episode-b").put("name", "第1话"))
            }).toString(), Charsets.UTF_8,
        )
        File(webDir, "_info.json").writeText(
            JSONObject().put("chapters", JSONArray().put(
                JSONObject().put("id", "episode-web").put("name", "第1话"),
            )).toString(), Charsets.UTF_8,
        )

        val chapters = OfflineStore.mangaChaptersFrom(root, "copymanga", comicId)
        assertEquals("同一通道有重复标题时保持 ID 区分，不能误合并真实话数", 3,
            chapters.size)
    }

    @Test
    fun offlineMangaPagesUseNumericOrderAndRejectInvalidFiles() {
        val root = tmpRoot()
        val chapter = File(root,
            "manga/downloads/jm/comic/chapter").apply { mkdirs() }
        val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte(), 0xe0.toByte()) +
            ByteArray(16)
        File(chapter, "0000.jpg").writeBytes(jpeg)
        File(chapter, "0001.jpg").writeBytes(jpeg)
        File(chapter, "0002.jpg").writeBytes(jpeg)
        File(chapter, "0003.jpg").writeText("not actually an image")
        val pages = OfflineStore.mangaChaptersFrom(root, "jm", "comic").single().second
        assertEquals(listOf("0000.jpg", "0001.jpg", "0002.jpg"), pages.map { it.name })
    }

    @Test
    fun offlineMangaPageIndexRemainsNumericPastFourDigits() {
        assertEquals(listOf(9999, 10000), listOf("10000.jpg", "9999.jpg")
            .mapNotNull(OfflineStore::pageIndex).sorted())
    }

    @Test
    fun manifestAndLegacyPageSequencesRejectPartialOfflineChapters() {
        val root = tmpRoot()
        val comic = File(root, "manga/downloads/jm/integrity")
        val expectedChapter = File(comic, "new-chapter").apply { mkdirs() }
        val oldChapter = File(comic, "legacy-chapter").apply { mkdirs() }
        val gappedLegacyChapter = File(comic, "gap-chapter").apply { mkdirs() }
        val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte(), 0xe0.toByte()) +
            ByteArray(16)
        File(expectedChapter, "0000.jpg").writeBytes(jpeg)
        File(expectedChapter, "0002.jpg").writeBytes(jpeg)
        File(oldChapter, "0000.jpg").writeBytes(jpeg)
        File(gappedLegacyChapter, "0000.jpg").writeBytes(jpeg)
        File(gappedLegacyChapter, "0002.jpg").writeBytes(jpeg)
        File(comic, "_info.json").writeText(
            JSONObject().put("chapters", JSONArray().put(
                JSONObject().put("id", "new-chapter").put("download_page_count", 3),
            )).toString(),
            Charsets.UTF_8,
        )

        val legacyOnly = OfflineStore.mangaFrom(root).single()
        assertEquals("旧清单中的连续本地章节继续兼容，内部缺页不得算完整", 1,
            legacyOnly.chapterCount)
        assertEquals("不完整的新章节不能进离线阅读目录",
            listOf("legacy-chapter"),
            OfflineStore.mangaChaptersFrom(root, "jm", "integrity").map { it.first })

        File(expectedChapter, "0001.jpg").writeBytes(jpeg)
        File(gappedLegacyChapter, "0001.jpg").writeBytes(jpeg)
        val all = OfflineStore.mangaFrom(root).single()
        assertEquals("补齐连续页序后，旧清单与新清单章节都可离线阅读", 3,
            all.chapterCount)
        val chapters = OfflineStore.mangaChaptersFrom(root, "jm", "integrity")
        assertEquals("清单章节按源目录优先；未登记的旧章节随后兼容保留",
            listOf("new-chapter", "gap-chapter", "legacy-chapter"),
            chapters.map { it.first })
        assertEquals(listOf("0000.jpg", "0001.jpg", "0002.jpg"),
            chapters.first().second.map { it.name })
    }

    @Test
    fun legacyCacheRootDownloadRequiresManifestWhileOrdinaryReadCacheIsExcluded() {
        val root = tmpRoot()
        val manga = File(root, "manga")
        val oldComic = File(manga, "_cache/copymanga/legacy-cache-download")
        val readOnlyComic = File(manga, "_cache/copymanga/ordinary-read-cache")
        val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte(), 0xe0.toByte()) +
            ByteArray(16)

        val legacyVolume = File(oldComic, "volume-1").apply { mkdirs() }
        File(legacyVolume, "0000.jpg").writeBytes(jpeg)
        File(legacyVolume, "0001.jpg").writeBytes(jpeg)
        File(oldComic, "_info.json").writeText(
            JSONObject().put("chapters", JSONArray().put(
                JSONObject().put("id", "volume-1").put("name", "第一卷")
                    .put("download_page_count", 2),
            )).toString(), Charsets.UTF_8,
        )
        File(readOnlyComic, "read-cache-only").apply { mkdirs() }
            .resolve("0000.jpg").writeBytes(jpeg)

        assertEquals("带清单的历史 cache-root 章节应可读",
            listOf("volume-1"), OfflineStore.mangaChaptersFrom(
                root, "copymanga", "legacy-cache-download",
            ).map { it.first })
        val shelf = OfflineStore.mangaFrom(root)
        assertEquals("舊 cache-root 下載清單必須保留，但普通線上閱讀 cache 不可冒充下載",
            listOf("legacy-cache-download"), shelf.map { it.comicId })
        assertEquals(1, shelf.single().chapterCount)
        assertEquals(2, shelf.single().imageCount)
        val chapters = OfflineStore.mangaChaptersFrom(
            root, "copymanga", "legacy-cache-download",
        )
        assertEquals(listOf("volume-1"), chapters.map { it.first })
        assertEquals(2, chapters.single().second.size)
        assertTrue("無下载清单的普通缓存不能进入本地目录",
            OfflineStore.mangaChaptersFrom(root, "copymanga", "ordinary-read-cache").isEmpty())
    }

    @Test
    fun offlineMangaVolumesPrecedeEpisodesUsingPersistedCatalogOrder() {
        val root = tmpRoot()
        val comic = File(root, "manga/downloads/copy/comic")
        val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte(), 0xe0.toByte()) +
            ByteArray(16)
        val chapterRows = listOf(
            "episode-1" to "第1话",
            "episode-2" to "第2话",
            "volume-1" to "第一卷",
            "volume-2" to "Vol.2",
            "appendix" to "01卷番外",
        )
        chapterRows.forEach { (id, _) ->
            File(comic, id).apply { mkdirs() }
                .resolve("0000.jpg").writeBytes(jpeg)
        }
        File(comic, "_info.json").writeText(
            JSONObject().put("chapters", JSONArray().apply {
                chapterRows.forEach { (id, name) ->
                    put(JSONObject().put("id", id).put("name", name))
                }
            }).toString(),
            Charsets.UTF_8,
        )

        assertEquals(
            listOf("volume-1", "volume-2", "episode-1", "episode-2", "appendix"),
            OfflineStore.mangaChaptersFrom(root, "copy", "comic").map { it.first },
        )
    }

    @Test
    fun legacyOfflineManifestUsesCanonicalNumericChapterOrder() {
        val root = tmpRoot()
        val comic = File(root, "manga/downloads/copy/legacy-order")
        val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte(), 0xe0.toByte()) +
            ByteArray(16)
        val chapterRows = listOf(
            "ep-10" to "第10话",
            "vol-2" to "第二卷",
            "vol-3" to "第三巻",
            "ep-2" to "第2話",
            "vol-1" to "第一卷",
            "ep-1" to "第1话",
            "appendix" to "番外2",
        )
        chapterRows.forEach { (id, _) ->
            File(comic, id).apply { mkdirs() }.resolve("0000.jpg").writeBytes(jpeg)
        }
        File(comic, "_info.json").writeText(
            JSONObject().put("chapters", JSONArray().apply {
                chapterRows.forEach { (id, name) ->
                    put(JSONObject().put("id", id).put("name", name))
                }
            }).toString(),
            Charsets.UTF_8,
        )

        assertEquals(
            listOf("vol-1", "vol-2", "vol-3", "ep-1", "ep-2", "ep-10", "appendix"),
            OfflineStore.mangaChaptersFrom(root, "copy", "legacy-order").map { it.first },
        )
    }

    @Test
    fun sharedChapterOrderFixtureMatchesServerCatalogOrder() {
        val fixtureText = requireNotNull(
            javaClass.getResourceAsStream("/manga_chapter_order_parity.json"),
        ) { "共享漫画章节排序夹具缺失" }.bufferedReader().use { it.readText() }
        val cases = JSONObject(fixtureText).getJSONArray("cases")
        val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte(), 0xe0.toByte()) +
            ByteArray(16)

        for (caseIndex in 0 until cases.length()) {
            val case = cases.getJSONObject(caseIndex)
            val comicId = "order-parity-${case.getString("name")}"
            val root = tmpRoot()
            val comic = File(root, "manga/downloads/copy/$comicId")
            val chapters = case.getJSONArray("chapters")
            val manifestRows = JSONArray()
            for (i in 0 until chapters.length()) {
                val row = chapters.getJSONObject(i)
                val id = row.getString("id")
                manifestRows.put(JSONObject(row.toString()))
                File(comic, id).apply { mkdirs() }.resolve("0000.jpg").writeBytes(jpeg)
            }
            File(comic, "_info.json").writeText(
                JSONObject().put("chapters", manifestRows).toString(), Charsets.UTF_8,
            )
            val expected = case.getJSONArray("expected").let { arr ->
                (0 until arr.length()).map(arr::getString)
            }
            try {
                assertEquals(
                    "Android 离线目录必须与共享服务端契约一致：${case.getString("name")}",
                    expected,
                    OfflineStore.mangaChaptersFrom(root, "copy", comicId).map { it.first },
                )
            } finally {
                root.deleteRecursively()
            }
        }
    }

    @Test
    fun downloadedManifestPageCountWinsOverStaleCacheManifest() {
        val fixtureText = requireNotNull(
            javaClass.getResourceAsStream("/manga_download_manifest_precedence.json"),
        ) { "下载清单优先级共享夹具缺失" }.bufferedReader().use { it.readText() }
        val cases = JSONObject(fixtureText).getJSONArray("cases")
        val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte(), 0xe0.toByte()) +
            ByteArray(16)

        for (index in 0 until cases.length()) {
            val case = cases.getJSONObject(index)
            val source = case.getString("source")
            val comicId = case.getString("comic_id")
            val chapterId = case.getString("chapter_id")
            val chapterName = case.getString("chapter_name")
            val expected = case.getInt("downloads_expected_page_count")
            val root = tmpRoot()
            val downloadComic = File(root, "manga/downloads/$source/$comicId")
            val chapterDir = File(downloadComic, chapterId).apply { mkdirs() }
            val indices = case.getJSONArray("page_indices")
            for (page in 0 until indices.length()) {
                File(chapterDir, "%04d.jpg".format(indices.getInt(page))).writeBytes(jpeg)
            }
            fun manifest(pageCount: Int) = JSONObject().put("chapters", JSONArray().put(
                JSONObject().put("id", chapterId).put("name", chapterName)
                    .put("download_page_count", pageCount),
            )).toString()
            File(downloadComic, "_info.json").writeText(manifest(expected), Charsets.UTF_8)
            File(root, "manga/_cache/$source/$comicId").apply { mkdirs() }
                .resolve("_info.json")
                .writeText(manifest(case.getInt("cache_expected_page_count")), Charsets.UTF_8)

            assertEquals(case.getBoolean("expected_complete"),
                listOf(chapterId) == OfflineStore.mangaChaptersFrom(
                    root, source, comicId,
                ).map { it.first })
            assertEquals(1, OfflineStore.mangaFrom(root).single().chapterCount)
            root.deleteRecursively()
        }
    }

    // ── 缓存键契约（与服务端 cache_key_of 逐字一致）────────────────────
    /**
     * 期望值全部由**服务端实现**算出（`engine/app_utils.py::cache_key_of`，Python）：
     * 磁盘上的缓存文件名是服务端写的；键不一致 → 离线阅读找不到文件、
     * 误报"这一章没有下载到本机"。
     *
     * 关键点：Python 的 `\w` 是 Unicode 感知的，而 Kotlin/Java 的 `\w` 只匹配 ASCII。
     * 旧实现只补了 `\u4e00-\u9fff`，于是假名 / 韩文 / CJK 扩展区都会算出不同的键。
     */
    @Test
    fun cacheKeyMatchesServerImplementation() {
        val cases = listOf(
            "https://www.kanshuw.com/107/107606/436633.html"
                to "https___www_kanshuw_com_107_107606_436633_html",
            "https://x.test/book/第1章.html" to "https___x_test_book_第1章_html",
            "https://ja.test/読/第1話.html" to "https___ja_test_読_第1話_html",
            "https://ko.test/소설/1화.html" to "https___ko_test_소설_1화_html",
            "https://ext.test/㐀/1.html" to "https___ext_test_㐀_1_html",
            "https://q.test/read?u=a&t=1#frag" to "https___q_test_read_u_a_t_1_frag",
            "https://full.test/第１章（上）.html" to "https___full_test_第１章_上__html",
            "https://t.test/read/1/26147420.html" to "https___t_test_read_1_26147420_html",
        )
        for ((url, expected) in cases) {
            assertEquals("键不一致会让离线找不到已下载的章：$url",
                expected, OfflineStore.cacheKeyOf(url))
        }
        // 60 字符截断同样要对齐（服务端 [-60:]）
        val long = "https://t.test/" + "a".repeat(200) + ".html"
        assertEquals(60, OfflineStore.cacheKeyOf(long).length)
        ev("缓存键契约：${cases.size} 条与 Python 端逐字一致")
    }

    @Test
    fun cacheKeyFindsRealCacheFileOnDisk() {
        // 端到端式：按服务端命名放一个缓存文件，客户端必须能按 URL 找回它
        val root = tmpRoot()
        val url = "https://ja.test/読/第1話.html"
        makeBookWithKeys(root, "book_ja", listOf(url))
        val dir = File(root, "books/book_ja")
        File(dir, OfflineStore.cacheKeyOf(url) + ".cache")
            .writeText("第1話\n\n正文。", Charsets.UTF_8)
        val idx = OfflineStore.novelDownloadedIndicesFrom(root, "book_ja")
        assertEquals("假名/汉字 URL 的缓存必须能找到", listOf(1), idx)
        val ch = OfflineStore.readNovelChapterFrom(root, "book_ja", 1)
        assertTrue("应读到正文：$ch", ch != null && ch.second.contains("正文"))
    }

    /** 按给定 URL 造一本书（不预置缓存，供"键契约"用例自己放文件） */
    private fun makeBookWithKeys(root: File, key: String, urls: List<String>) {
        val dir = File(root, "books/$key")
        dir.mkdirs()
        val arr = JSONArray()
        urls.forEachIndexed { i, u ->
            arr.put(JSONObject().put("name", "第${i + 1}章").put("url", u))
        }
        File(dir, "_state.json").writeText(
            JSONObject().put("book", JSONObject().put("name", key))
                .put("chapters", arr).toString(), Charsets.UTF_8)
    }
}
