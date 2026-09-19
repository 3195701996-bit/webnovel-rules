package com.webnovel.mobile

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * **阅读进度必须真的被用上**（用户现场反馈："明明记了进度，重进却从第一话开始"）。
 *
 * 用夹具漫画（书库 + 已下载一话，因此详情走**服务端 quick 路径**——正是用户最常读的
 * 那条路径）验证三件事：
 *   1. 详情响应必须带 `source` / `comic_id`：客户端用它们去历史里找阅读进度，
 *      此前 quick 路径只给 `id`，客户端读到空串 → 匹配失败 → 续读落点退回第一话；
 *   2. 写入"第 2 话"的进度后，详情页的续读落点必须是第 2 话（不是第 1 话）；
 *   3. 界面上点「继续阅读」进阅读器，顶栏显示的必须是第 2 话。
 *
 * 另外回答用户"是否清理缓存导致"：`ClearingDoesNotTouchProgress` 那条断言由桌面用例
 * （tests/test_manga_progress_identity.py）覆盖——历史/书库文件不在任何被清理的目录里，
 * 且跑完全部清理路径后两文件字节不变。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class MangaResumeProgressTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext
    private lateinit var comic: SelfTestComic
    private lateinit var gateway: EngineGateway

    private fun ev(line: String) = println("RESUME_PROGRESS_EVIDENCE $line")

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size
    private fun texts(t: String, substring: Boolean = false) =
        rule.onAllNodesWithText(t, substring = substring).fetchSemanticsNodes().size

    private fun waitText(t: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) { texts(t, substring) > 0 }
    }

    @Before
    fun setUp() {
        val uniq = "__selftest_resume_%d__".format(System.currentTimeMillis() % 1_000_000)
        comic = SelfTestComic(comicId = uniq)
        comic.create()
        gateway = EngineGateway(ctx)
        val st = runBlocking { gateway.connect() }
        assertTrue("引擎未就绪：$st", st is EngineState.Ready)
    }

    @After
    fun tearDown() {
        // 清掉本用例写的阅读进度（避免污染后续用例的书架）
        val ep = runBlocking { gateway.currentEndpoint() }
        if (ep != null) {
            runBlocking {
                runCatching {
                    gateway.httpPost(ep.port, "/api/manga/history",
                        JSONObject().put("source", comic.sourceKey)
                            .put("comic_id", comic.comicIdValue)
                            .put("idx", 0).put("pos", "").put("title", SelfTestComic.TITLE)
                            .toString())
                }
            }
        }
        comic.cleanup()
        assertTrue("自检漫画未清理干净", !comic.exists())
    }

    @Test
    fun savedProgressIsHonoredByDetailAndReader() {
        val ep = runBlocking { gateway.currentEndpoint() } ?: throw AssertionError("引擎未就绪")

        // 1) 详情必须带身份字段（quick 路径曾缺 comic_id）
        val det = runBlocking {
            gateway.httpText(ep.port, "/api/manga/${comic.sourceKey}/${comic.comicIdValue}")
        }
        assertTrue("详情应 200：HTTP ${det.code}", det.ok)
        val d = JSONObject(det.body)
        ev("详情：quick=${d.optBoolean("quick")} source=${d.optString("source")} " +
            "comic_id=${d.optString("comic_id")}")
        assertEquals("详情必须回显请求的 source", comic.sourceKey, d.optString("source"))
        assertEquals("详情必须回显 comic_id（客户端靠它匹配进度）",
            comic.comicIdValue, d.optString("comic_id"))

        // 2) 写入"第 2 话"的进度
        val save = runBlocking {
            gateway.httpPost(ep.port, "/api/manga/history",
                JSONObject().put("source", comic.sourceKey)
                    .put("comic_id", comic.comicIdValue)
                    .put("idx", 1)                       // 0 基：第 2 话
                    .put("pos", "${SelfTestComic.CH2_NAME} P3")
                    .put("title", SelfTestComic.TITLE).toString())
        }
        assertTrue("进度保存应成功：HTTP ${save.code} ${save.body.take(80)}", save.ok)

        // 3) 详情页的续读落点必须是第 2 话（客户端 resumeIndex 的同一判据）
        val detail = EngineData.mangaDetail(det.body)!!
        val hist = runBlocking { gateway.httpText(ep.port, "/api/manga/history") }
        val readIdx = EngineData.mangaHistory(hist.body)
            .firstOrNull {
                it.source == detail.source.ifBlank { comic.sourceKey } &&
                    it.comicId == detail.comicId.ifBlank { comic.comicIdValue }
            }?.idx ?: -1
        assertEquals("历史里必须能按 (source, comic_id) 匹配到刚写的进度", 1, readIdx)
        assertEquals("续读落点应为第 2 话", 1, detail.resumeIndex(readIdx))
        ev("历史匹配 idx=$readIdx；续读落点=${detail.resumeIndex(readIdx)}（第 2 话）")

        // 4) 界面上点「继续阅读」→ 阅读器必须是第 2 话
        waitText(SelfTestComic.TITLE)
        rule.onAllNodesWithText(SelfTestComic.TITLE)[0].performScrollTo().performClick()
        waitText("继续阅读", timeoutMs = 60_000, substring = true)
        ev("详情页按钮：继续阅读（承接进度）")
        rule.onAllNodesWithText("继续阅读", substring = true)[0].performClick()
        // 阅读器顶栏是「<漫画标题> · <话名>」的**合并字符串**，所以按子串断言
        // （此前用精确匹配永远等不到，实测踩到）
        rule.waitUntil(60_000) { texts(SelfTestComic.CH2_NAME, substring = true) > 0 }
        assertTrue("阅读器必须停在第 2 话（顶栏应含『${SelfTestComic.CH2_NAME}』）",
            texts(SelfTestComic.CH2_NAME, substring = true) > 0)
        assertEquals("不得退回第 1 话（顶栏不应出现『${SelfTestComic.CH1_NAME}』）",
            0, texts(SelfTestComic.CH1_NAME, substring = true))
        ev("阅读器顶栏含『${SelfTestComic.CH2_NAME}』且不含第 1 话——进度被真正用上了")
        Unit
    }

    /**
     * 用户现场复现的那一条：**在第 N 话退出，再进来却跳到第 1 话**。
     *
     * 用一个"旧式"记录把当时的处境复刻出来：`idx` 越界（目录已变化）+ 只有章名、
     * 没有 chapter_id。旧代码遇到越界就退回"第一个已下载话/第 1 话"，而且进阅读器
     * 后会自动把这个落点写回记录 —— 于是记录被永久改成第 1 话。
     *
     * 现在要求：① 详情按**章名**定位到第 2 话（exact=true）；② 阅读器落在第 2 话；
     * ③ 实时保存必须写"第 2 话 + 章节身份"，**不能**把记录覆盖成第 1 话。
     */
    @Test
    fun staleIndexStillResumesRecordedChapterAndRecordsIt() {
        val ep = runBlocking { gateway.currentEndpoint() } ?: throw AssertionError("引擎未就绪")

        // 1) 复刻旧式记录：下标越界 + 只有章名
        val save = runBlocking {
            gateway.httpPost(ep.port, "/api/manga/history",
                JSONObject().put("source", comic.sourceKey)
                    .put("comic_id", comic.comicIdValue)
                    .put("idx", 999)                                   // 越界（目录已变）
                    .put("pos", "${SelfTestComic.CH2_NAME} P3")         // 只剩章名可信
                    .put("title", SelfTestComic.TITLE).toString())
        }
        assertTrue("旧式记录写入应成功", save.ok)

        // 2) 详情必须按身份解析出第 2 话（而不是退回第 1 话）
        val det = runBlocking {
            gateway.httpText(ep.port, "/api/manga/${comic.sourceKey}/${comic.comicIdValue}")
        }
        assertTrue("详情应 200：HTTP ${det.code}", det.ok)
        val d = EngineData.mangaDetail(det.body)!!
        val res = d.resume
        assertTrue("详情必须给出 resume（服务端按身份解析）", res != null)
        assertEquals("越界下标必须靠章名定位到第 2 话（0 基下标 1）", 1, res!!.index)
        assertTrue("章名精确命中应标为精确：by=${res.by}", res.exact)
        assertEquals("页码也要还原", 3, res.page)
        ev("旧式记录(idx=999, pos='${SelfTestComic.CH2_NAME} P3') → resume.index=${res.index} " +
            "by=${res.by} exact=${res.exact} page=${res.page}")

        // 3) 界面上进阅读器 → 必须是第 2 话
        waitText(SelfTestComic.TITLE)
        rule.onAllNodesWithText(SelfTestComic.TITLE)[0].performScrollTo().performClick()
        waitText("继续阅读", timeoutMs = 60_000, substring = true)
        rule.onAllNodesWithText("继续阅读", substring = true)[0].performClick()
        rule.waitUntil(60_000) { texts(SelfTestComic.CH2_NAME, substring = true) > 0 }
        assertTrue("阅读器必须停在第 2 话（顶栏应含『${SelfTestComic.CH2_NAME}』）",
            texts(SelfTestComic.CH2_NAME, substring = true) > 0)
        assertEquals("不得退回第 1 话（顶栏不应出现『${SelfTestComic.CH1_NAME}』）",
            0, texts(SelfTestComic.CH1_NAME, substring = true))
        ev("阅读器落点=第 2 话（未退回第 1 话）")

        // 4) 实时记录：几秒内记录应被写成"第 2 话 + 章节身份"，且不能变成第 1 话
        var rec: JSONObject? = null
        rule.waitUntil(20_000) {
            val h = runBlocking { gateway.httpText(ep.port, "/api/manga/history") }
            rec = EngineData.mangaHistory(h.body).firstOrNull {
                it.comicId == comic.comicIdValue
            }?.let {
                JSONObject().put("idx", it.idx).put("pos", it.pos)
            }
            h.ok && (rec?.optInt("idx") ?: -1) == 1
        }
        ev("实时写入的记录：idx=${rec?.optInt("idx")} pos=${rec?.optString("pos")}")
        assertEquals("记录必须停在第 2 话（0 基下标 1）", 1, rec?.optInt("idx"))
        assertTrue("记录里的章名应是第 2 话：${rec?.optString("pos")}",
            rec?.optString("pos")?.contains(SelfTestComic.CH2_NAME) == true)
        Unit
    }
}
