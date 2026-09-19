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
        for (i in 1..3) File(dir, String.format("%04d.avif", i)).writeText("x")
        val lib = JSONArray().put(JSONObject().put("source", "copymanga")
            .put("comic_id", "comic_avif").put("title", "全 AVIF 的一话"))
        File(root, "manga/_library.json").writeText(lib.toString(), Charsets.UTF_8)
        val list = OfflineStore.mangaFrom(root)
        assertEquals("纯 avif 章节必须被统计，否则整部漫画会从离线书架消失", 1, list.size)
        assertEquals(3, list.single().imageCount)
        assertEquals("全 AVIF 的一话", list.single().title)
        ev("纯 avif 章节：${list.single().label}")
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
