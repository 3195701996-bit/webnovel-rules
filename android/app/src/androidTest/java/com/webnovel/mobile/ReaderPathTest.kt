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
 * 原生阅读链路的数据契约验收（设备上跑真实引擎，**不依赖外部书源与网络**）：
 *
 *   书架概览 → 详情（目录/下载状态/失败原因） → 章节正文（已下载/未下载两条分支）
 *   → 阅读进度写入与读回 → 书架带出进度（"继续阅读"的数据来源）
 *
 * 用合成书保证可重复；结束清理干净，不留在书库与进度文件里。
 * 走的是生产代码路径：EngineGateway.connect() + httpText/httpPost + EngineData 解析。
 */
@RunWith(AndroidJUnit4::class)
class ReaderPathTest {

    private lateinit var book: SelfTestBook
    private lateinit var gateway: EngineGateway

    private fun ev(line: String) = println("READER_PATH_EVIDENCE $line")

    @Before
    fun setUp() {
        val ctx = InstrumentationRegistry.getInstrumentation().targetContext
        book = SelfTestBook()
        book.create()
        gateway = EngineGateway(ctx)
        val state = runBlocking { gateway.connect() }
        assertTrue("引擎未就绪：$state", state is EngineState.Ready)
        ev("引擎就绪 instance=${(state as EngineState.Ready).endpoint.instanceId.take(8)}")
    }

    @After
    fun tearDown() {
        runCatching { runBlocking { gateway.stopEngine() } }
        book.cleanup()
        assertFalse("自检书目录未清理干净", book.exists())
        assertEquals("阅读进度里有自检残留", 0, book.progressEntryCount())
        ev("清理完成 自检书存在=${book.exists()} 进度残留=${book.progressEntryCount()}")
    }

    @Test
    fun readerPath_contractAndProgress() = runBlocking {
        val ep = gateway.currentEndpoint()!!

        // 1) 书架列表带出这本书
        val list = gateway.httpText(ep.port, "/api/books")
        assertEquals("书架接口状态码", 200, list.code)
        val items = EngineData.novels(list.body)
        val mine = items.firstOrNull { it.key == book.bookKey }
        assertNotNull("书架未列出自检书：${list.body.take(200)}", mine)
        assertEquals("章节总数", 3, mine!!.total)
        assertEquals("已下载章节数", 1, mine.done)
        ev("书架：total=3 done=1 percent=${mine.percent} 已读=${mine.started}")

        // 2) 详情：目录、下载状态、失败原因
        val det = gateway.httpText(ep.port, book.detailUrl)
        assertEquals("详情状态码", 200, det.code)
        val d = EngineData.bookDetail(det.body)
        assertNotNull("详情解析失败：${det.body.take(200)}", d)
        assertEquals("书名", SelfTestBook.BOOK_NAME, d!!.name)
        assertEquals("作者", "自检", d.author)
        assertTrue("简介应为非空", d.intro.isNotBlank())
        assertEquals("目录条数", 3, d.chapters.size)
        assertTrue("第 1 章应标记已下载", d.chapters[0].downloaded)
        assertFalse("第 2 章不应标记已下载", d.chapters[1].downloaded)
        assertTrue("第 3 章应标记失败", d.chapters[2].failed)
        assertEquals("失败原因", SelfTestBook.FAILED_REASON, d.chapters[2].failedReason)
        // 详情里的 progress 只能当作**抓取任务进度**解读，不得当阅读位置
        assertEquals("抓取任务状态", "paused", d.crawlStatus)
        assertEquals("抓取任务已完成数", 1, d.crawlCompleted)
        val noProg = EngineData.readProgress(gateway.httpText(ep.port, book.progressUrl).body)
        assertEquals("此时应无阅读进度", 0, noProg?.idx ?: 0)
        assertEquals("继续阅读落点（无进度→首个已下载章）", 1, d.resumeIndex(noProg))
        ev("详情：chapters=3 已下载=${d.chapters.count { it.downloaded }} 失败=${d.failedCount} 抓取=${d.crawlStatus}")

        // 3) 已下载章节：正文可取，prev/next 与 total 正确
        val c1 = gateway.httpText(ep.port, book.chapter1Url)
        assertEquals("章节状态码", 200, c1.code)
        val ch1 = EngineData.chapter(c1.body)
        assertNotNull("章节解析失败", ch1)
        assertTrue("第 1 章 downloaded 应为 true", ch1!!.downloaded)
        assertTrue("正文应含第一段", ch1.content.contains(SelfTestBook.CHAPTER1_BODY_1))
        assertTrue("正文应含第二段", ch1.content.contains(SelfTestBook.CHAPTER1_BODY_2))
        assertEquals("段落数（缓存首行是章节名，已去重）", 2, ch1.paragraphs.size)
        assertEquals("首段应为正文第一段", SelfTestBook.CHAPTER1_BODY_1, ch1.paragraphs[0])
        assertEquals("章节名", SelfTestBook.CHAPTER1_NAME, ch1.name)
        assertEquals("prev 应为空", null, ch1.prev)
        assertEquals("next", 2, ch1.next)
        assertEquals("total", 3, ch1.total)
        ev("第1章：downloaded=true 段落=${ch1.paragraphs.size} next=${ch1.next}")

        // 4) 未下载章节：必须如实返回 downloaded=false 且正文为空（不能假装有内容）
        val c2 = gateway.httpText(ep.port, book.chapter2Url)
        assertEquals("未下载章节状态码", 200, c2.code)
        val ch2 = EngineData.chapter(c2.body)
        assertNotNull(ch2)
        assertFalse("第 2 章 downloaded 应为 false", ch2!!.downloaded)
        assertEquals("未下载章节正文应为空", "", ch2.content)
        ev("第2章：downloaded=false 正文长度=${ch2.content.length}")

        // 5) 越界章节 → 404（客户端据此显示可读错误，而不是空正文）
        val over = gateway.httpText(ep.port, "/api/books/${book.bookKey}/chapter/4")
        assertEquals("越界章节状态码", 404, over.code)

        // 6) 阅读进度写入并读回
        val save = gateway.httpPost(
            ep.port, book.progressUrl,
            JSONObject().put("idx", 2).put("pct", 37).put("name", SelfTestBook.CHAPTER2_NAME).toString(),
        )
        assertEquals("进度保存状态码", 200, save.code)
        assertTrue("进度保存应返回 ok", save.body.contains("\"ok\": true") || save.body.contains("\"ok\":true"))
        val got = gateway.httpText(ep.port, book.progressUrl)
        val p = JSONObject(got.body)
        assertEquals("读回 idx", 2, p.optInt("idx"))
        assertEquals("读回 pct", 37, p.optInt("pct"))
        assertEquals("读回章名", SelfTestBook.CHAPTER2_NAME, p.optString("name"))
        ev("进度：idx=${p.optInt("idx")} pct=${p.optInt("pct")}")

        // 7) 书架带出进度（"继续阅读"的数据来源）
        val list2 = EngineData.novels(gateway.httpText(ep.port, "/api/books").body)
        val mine2 = list2.firstOrNull { it.key == book.bookKey }!!
        assertEquals("书架 read_idx", 2, mine2.readIdx)
        assertEquals("书架 read_pct", 37, mine2.readPct)
        assertEquals("书架 read_name", SelfTestBook.CHAPTER2_NAME, mine2.readName)
        assertTrue("应视为已开始阅读", mine2.started)
        // 详情页据此把"开始阅读"变成"继续阅读 第 2 章"：
        // 阅读位置必须取自全局进度端点，而不是详情里那份抓取任务进度
        val d2 = EngineData.bookDetail(gateway.httpText(ep.port, book.detailUrl).body)!!
        val prog2 = EngineData.readProgress(gateway.httpText(ep.port, book.progressUrl).body)!!
        assertEquals("阅读进度 idx", 2, prog2.idx)
        assertEquals("阅读进度 pct", 37, prog2.pct)
        assertEquals("阅读进度章名", SelfTestBook.CHAPTER2_NAME, prog2.name)
        assertTrue("应视为已开始阅读", prog2.started)
        assertEquals("详情 resumeIndex 应跟随阅读进度", 2, d2.resumeIndex(prog2))
        assertEquals("抓取任务进度不得被当作阅读位置", "paused", d2.crawlStatus)
        ev("书架进度：read_idx=${mine2.readIdx} read_pct=${mine2.readPct} resume=${d2.resumeIndex(prog2)}")

        // 8) 非法进度（idx<1）必须被服务端拒绝——客户端也不应发这种写入
        val bad = gateway.httpPost(ep.port, book.progressUrl, JSONObject().put("idx", 0).toString())
        assertEquals("idx=0 应被拒绝", 400, bad.code)
        val still = JSONObject(gateway.httpText(ep.port, book.progressUrl).body)
        assertEquals("非法写入不得覆盖既有进度", 2, still.optInt("idx"))

        // 9) 路径穿越防护仍然生效
        val traversal = gateway.httpText(ep.port, "/api/books/..%2f..%2fetc")
        assertEquals("路径穿越应 404", 404, traversal.code)
    }
}
