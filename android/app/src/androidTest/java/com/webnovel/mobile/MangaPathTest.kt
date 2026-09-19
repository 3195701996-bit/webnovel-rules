package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 原生漫画阅读链路的数据契约验收（设备上跑真实引擎，命中本地已下载文件，
 * **不请求任何源站**）：
 *
 *   书库列表 → 详情（章节 + 已下载集合） → 本话图片数（local=true）
 *   → 图片字节（本地直出、内容类型为 image 且非空） → 阅读进度写入/读回 → 书库带出进度
 *
 * 用合成漫画保证可重复；结束清理干净。
 */
@RunWith(AndroidJUnit4::class)
class MangaPathTest {

    private lateinit var comic: SelfTestComic
    private lateinit var gateway: EngineGateway

    private fun ev(line: String) = println("MANGA_PATH_EVIDENCE $line")

    @Before
    fun setUp() {
        val ctx = InstrumentationRegistry.getInstrumentation().targetContext
        comic = SelfTestComic()
        comic.create()
        gateway = EngineGateway(ctx)
        val state = runBlocking { gateway.connect() }
        assertTrue("引擎未就绪：$state", state is EngineState.Ready)
        ev("引擎就绪 instance=${(state as EngineState.Ready).endpoint.instanceId.take(8)}")
        // 合成图片必须大于服务端直出门槛（1000 字节）
        assertTrue("合成图片太小，服务端不会直出", comic.imageSize(SelfTestComic.CH1_ID, 0) > 1000)
    }

    @After
    fun tearDown() {
        runCatching { runBlocking { gateway.stopEngine() } }
        comic.cleanup()
        assertFalse("自检漫画目录未清理干净", comic.exists())
        assertEquals("书库里有自检残留", 0, comic.libraryEntryCount())
        assertEquals("历史里有自检残留", 0, comic.historyEntryCount())
        ev("清理完成 书库残留=${comic.libraryEntryCount()} 历史残留=${comic.historyEntryCount()}")
    }

    @Test
    fun mangaPath_contractAndProgress() = runBlocking {
        val ep = gateway.currentEndpoint()!!

        // 1) 书库列表带出这部漫画
        val lib = gateway.httpText(ep.port, "/api/manga/library")
        assertEquals("书库接口状态码", 200, lib.code)
        val items = EngineData.manga(lib.body)
        val mine = items.firstOrNull { it.source == comic.sourceKey && it.comicId == comic.comicIdValue }
        assertNotNull("书库未列出自检漫画：${lib.body.take(200)}", mine)
        assertEquals("标题", SelfTestComic.TITLE, mine!!.title)
        ev("书库：title=${mine.title} local_images=${mine.localImages} chapters=${mine.dlTotal}")

        // 2) 详情：章节列表 + 已下载集合（第 3 话刻意未下载）
        val det = gateway.httpText(ep.port, comic.detailUrl)
        assertEquals("详情状态码", 200, det.code)
        val d = EngineData.mangaDetail(det.body)
        assertNotNull("详情解析失败：${det.body.take(200)}", d)
        assertEquals("标题", SelfTestComic.TITLE, d!!.title)
        assertEquals("章节数", 3, d.chapters.size)
        assertEquals("第 1 话 id", SelfTestComic.CH1_ID, d.chapters[0].id)
        assertEquals("第 1 话名", SelfTestComic.CH1_NAME, d.chapters[0].name)
        assertTrue("第 1 话应为已下载", d.downloaded.contains(SelfTestComic.CH1_ID))
        assertTrue("第 2 话应为已下载", d.downloaded.contains(SelfTestComic.CH2_ID))
        assertFalse("第 3 话不应标记已下载", d.downloaded.contains(SelfTestComic.CH3_ID))
        assertEquals("已下载话数", 2, d.downloadedCount)
        ev("详情：chapters=3 已下载=${d.downloadedCount} 第3话未下载=${!d.downloaded.contains(SelfTestComic.CH3_ID)}")

        // 3) 无历史时继续阅读落点 = 第一个已下载话
        assertEquals("继续阅读落点", 0, d.resumeIndex(-1))

        // 4) 本话图片数：命中本地（local=true）
        val ch1 = gateway.httpText(ep.port, comic.chapterUrl(SelfTestComic.CH1_ID))
        assertEquals("章节接口状态码", 200, ch1.code)
        val pages = EngineData.mangaChapterPages(ch1.body)
        assertNotNull("章节解析失败：${ch1.body.take(200)}", pages)
        assertEquals("第 1 话页数", 2, pages!!.count)
        assertTrue("应标记为本地图片", pages.local)
        ev("第1话：count=${pages.count} local=${pages.local}")

        // 5) 图片字节：本地直出，类型为图片且非空（离线可读的关键）
        val img = rawGet(ep.port, comic.imageUrl(SelfTestComic.CH1_ID, 0), ep.token)
        assertEquals("图片状态码", 200, img.first)
        val ctype = img.third["Content-Type"] ?: ""
        assertTrue("内容类型应为图片，实际=$ctype", ctype.startsWith("image/"))
        assertTrue("图片字节过小：${img.second.size}", img.second.size > 1000)
        ev("图片：idx=0 status=200 type=$ctype bytes=${img.second.size}")

        // 6) 阅读进度写入并读回（与网页端共用 _history.json 的 <章名> P<页> 约定）
        val save = gateway.httpPost(
            ep.port, "/api/manga/history",
            JSONObject().put("source", comic.sourceKey).put("comic_id", comic.comicIdValue)
                .put("idx", 0).put("pos", EngineData.mangaPos(SelfTestComic.CH1_NAME, 2))
                .put("title", SelfTestComic.TITLE).toString(),
        )
        assertEquals("历史保存状态码", 200, save.code)
        assertTrue("历史保存应返回 ok",
            save.body.contains("\"ok\": true") || save.body.contains("\"ok\":true"))

        val hist = EngineData.mangaHistory(gateway.httpText(ep.port, "/api/manga/history").body)
        val h = hist.firstOrNull { it.source == comic.sourceKey && it.comicId == comic.comicIdValue }
        assertNotNull("历史里没有自检漫画", h)
        assertEquals("历史 idx", 0, h!!.idx)
        assertEquals("历史 pos", "${SelfTestComic.CH1_NAME} P2", h.pos)
        assertEquals("从 pos 取页码", 2, EngineData.pageFromPos(h.pos))
        ev("历史：idx=${h.idx} pos=${h.pos} page=${EngineData.pageFromPos(h.pos)}")

        // 7) 书库带出进度（书架"继续阅读"的数据来源）
        val lib2 = EngineData.manga(gateway.httpText(ep.port, "/api/manga/library").body)
        val mine2 = lib2.firstOrNull { it.source == comic.sourceKey && it.comicId == comic.comicIdValue }!!
        assertEquals("书库 read_idx", 0, mine2.readIdx)
        assertEquals("书库 read_pos", "${SelfTestComic.CH1_NAME} P2", mine2.readPos)
        assertTrue("应视为已开始阅读", mine2.started)
        ev("书库进度：read_idx=${mine2.readIdx} read_pos=${mine2.readPos}")

        // 8) 有历史时详情落点跟随历史
        val d2 = EngineData.mangaDetail(gateway.httpText(ep.port, comic.detailUrl).body)!!
        assertEquals("有历史后继续阅读落点", 0, d2.resumeIndex(mine2.readIdx))

        // 9) 参数校验：缺 source/comic_id 时必须明确失败，而不是写进一条空记录
        val bad = gateway.httpPost(ep.port, "/api/manga/history",
            JSONObject().put("idx", 1).toString())
        assertFalse("缺参数不应返回 2xx", bad.ok)
        ev("缺参数请求被拒：HTTP ${bad.code}")
    }

    /** 需要看响应头与原始字节（图片不是文本），故单独发一次请求 */
    private fun rawGet(port: Int, path: String, token: String): Triple<Int, ByteArray, Map<String, String>> {
        val c = java.net.URL("http://127.0.0.1:$port$path").openConnection() as java.net.HttpURLConnection
        return try {
            c.connectTimeout = 8000
            c.readTimeout = 30000
            c.setRequestProperty("X-Mobile-Token", token)
            val code = c.responseCode
            val bytes = (if (code in 200..299) c.inputStream else c.errorStream)
                ?.use { it.readBytes() } ?: ByteArray(0)
            val headers = c.headerFields.entries
                .filter { it.key != null }
                .associate { it.key to (it.value.firstOrNull() ?: "") }
            Triple(code, bytes, headers)
        } finally {
            runCatching { c.disconnect() }
        }
    }
}
