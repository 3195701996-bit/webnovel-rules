package com.webnovel.mobile

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * **缓存与进度不一致时必须自愈、且界面照实说明原因**（0.55.0，P1-3 实测发现）。
 *
 * 起因：小说最小回归源集在设备上复跑时，两个源的第 1 章正文变成 0 字——
 * 书目录被删掉时任务还在跑，任务线程随后用内存状态把目录写回，
 * 于是 `_state.json.completed` 里留着第 1 章，而第 1 章的 `.cache` 已经不在了。
 * 后果：书架显示"已下载 N 章"、点进去正文空白、且**"继续下载"永远跳过这一章**。
 *
 * 本用例用合成书（不联网、确定性）锁住修复后的契约：
 *   1. 详情/书库的"已下载"只看磁盘缓存：`done`=1（不是 state 说的 2）、
 *      `stale_completed`=1 暴露不一致、第 2 章 `downloaded=false`；
 *   2. 单章端点内容为空时给 `reason`：缓存丢失与"尚未下载"必须分开说；
 *   3. 阅读器把服务端原因显示出来（`reader_missing_reason`），旁边就是「在线获取本章」。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class StaleCacheHealTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext
    private lateinit var gateway: EngineGateway
    private lateinit var bookDir: File

    private fun ev(line: String) = println("STALE_CACHE_EVIDENCE $line")

    private val key = "自检_stale_cache"

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size
    private fun texts(t: String, substring: Boolean = false) =
        rule.onAllNodesWithText(t, substring = substring).fetchSemanticsNodes().size

    @Before
    fun setUp() {
        val booksDir = File(ctx.filesDir, "runtime/books")
        bookDir = File(booksDir, key)
        bookDir.deleteRecursively()
        require(bookDir.mkdirs()) { "无法创建合成书目录：$bookDir" }
        // 第 1 章：有缓存 + 在 completed；第 2 章：**在 completed 但没有缓存**（不一致）；
        // 第 3 章：既不在 completed 也没有缓存（真正的"尚未下载"）
        File(bookDir, "_state.json").writeText(
            JSONObject().apply {
                put("book", JSONObject().apply {
                    put("name", BOOK_NAME)
                    put("author", "自检")
                    put("intro", "缓存与进度不一致的合成书。")
                    put("source_uid", "selftest")
                    put("book_url", "https://selftest.invalid/stale/")
                })
                put("chapters", JSONArray().apply {
                    put(JSONObject().put("name", CH1).put("url", URL1))
                    put(JSONObject().put("name", CH2).put("url", URL2))
                    put(JSONObject().put("name", CH3).put("url", URL3))
                })
                put("completed", JSONArray().put(URL1).put(URL2))
                put("failed", JSONObject())
                put("updated_at", "2026-01-01T00:00:00")
            }.toString(), Charsets.UTF_8)
        File(bookDir, cacheKeyOf(URL1) + ".cache")
            .writeText("$CH1\n\n$CH1_BODY\n", Charsets.UTF_8)

        gateway = EngineGateway(ctx)
        val st = runBlocking { gateway.connect() }
        assertTrue("引擎未就绪：$st", st is EngineState.Ready)
    }

    @After
    fun tearDown() {
        val ep = runBlocking { gateway.currentEndpoint() }
        if (ep != null) runBlocking { runCatching { gateway.httpDelete(ep.port, "/api/books/$key") } }
        bookDir.deleteRecursively()
        File(ctx.filesDir, "runtime/trash").listFiles()
            ?.filter { it.name.startsWith(key) }?.forEach { it.deleteRecursively() }
        assertFalse("合成书目录未清理干净", bookDir.exists())
    }

    @Test
    fun api_reportsDiskTruthAndReason() = runBlocking {
        val ep = gateway.currentEndpoint()!!

        // 1) 详情：已下载按磁盘算（1 章），并把 state 与磁盘的差异暴露出来
        val det = gateway.httpText(ep.port, "/api/books/$key")
        assertEquals("详情状态码", 200, det.code)
        val o = JSONObject(det.body)
        assertEquals("已下载应等于磁盘上真实存在的缓存数", 1, o.optInt("done"))
        assertEquals("state 里记录的完成数", 2, o.optInt("state_completed"))
        assertEquals("state 说完成、磁盘没有的章节数", 1, o.optInt("stale_completed"))
        val chapters = o.optJSONArray("chapters")!!
        assertTrue("第 1 章有缓存 → 已下载", chapters.optJSONObject(0).optBoolean("downloaded"))
        assertFalse("第 2 章缓存丢失 → 不得标已下载（此前靠 completed 谎报）",
            chapters.optJSONObject(1).optBoolean("downloaded"))
        ev("详情：done=${o.optInt("done")} state_completed=${o.optInt("state_completed")} " +
            "stale_completed=${o.optInt("stale_completed")}")

        // 2) 书库列表同口径
        val lib = JSONObject(gateway.httpText(ep.port, "/api/books").body)
        val arr = lib.optJSONArray("books")!!
        var mine: JSONObject? = null
        for (i in 0 until arr.length()) {
            if (arr.optJSONObject(i)?.optString("key") == key) mine = arr.optJSONObject(i)
        }
        assertNotNull("书库未列出合成书", mine)
        assertEquals("书库 done 与详情同口径", 1, mine!!.optInt("done"))
        ev("书库：done=${mine.optInt("done")} total=${mine.optInt("total")}")

        // 3) 单章端点：缓存丢失 vs 尚未下载，原因必须分开
        val c2 = JSONObject(gateway.httpText(ep.port, "/api/books/$key/chapter/2").body)
        assertEquals("第 2 章 downloaded", false, c2.optBoolean("downloaded"))
        assertEquals("第 2 章正文应为空", "", c2.optString("content"))
        val r2 = c2.optString("reason")
        assertTrue("缓存丢失必须给出可读原因（实际：$r2）", r2.contains("缓存"))
        assertTrue("原因里要指出可重试（实际：$r2）", r2.contains("重试"))
        val c3 = JSONObject(gateway.httpText(ep.port, "/api/books/$key/chapter/3").body)
        assertEquals("真正未下载的原因", "本章尚未下载", c3.optString("reason"))
        ev("第2章原因：$r2 ｜ 第3章原因：${c3.optString("reason")}")

        // 4) 有缓存的章节不受影响
        val c1 = JSONObject(gateway.httpText(ep.port, "/api/books/$key/chapter/1").body)
        assertTrue("第 1 章仍可读", c1.optBoolean("downloaded") && c1.optString("content").contains(CH1_BODY))
    }

    @Test
    fun readerShowsServerReasonForMissingCache() {
        // 书架 → 合成书 → 详情：头部必须显示磁盘口径的"已下载 1 章"
        rule.waitUntil(150_000) { texts(BOOK_NAME) > 0 }
        rule.onAllNodesWithText(BOOK_NAME)[0].performScrollTo().performClick()
        rule.waitUntil(60_000) { texts("目录 3 章 · 已下载 1 章") > 0 }
        ev("详情头部：目录 3 章 · 已下载 1 章（磁盘口径）")

        // 点第 2 章（缓存丢失那一章）→ 阅读器必须说明原因，而不是空白页
        rule.onAllNodesWithText(CH2)[0].performScrollTo().performClick()
        rule.waitUntil(60_000) { nodes("reader_missing_reason") > 0 }
        val shown = rule.onNodeWithTag("reader_missing_reason")
            .fetchSemanticsNode().config.toString()
        ev("阅读器显示原因：$shown")
        assertTrue("阅读器要把服务端原因显示出来（缓存丢失），实际：$shown",
            shown.contains("缓存"))
        assertTrue("必须同时给出「在线获取本章」自救入口",
            texts("在线获取本章") > 0)
        ev("阅读器：原因文案 + 在线获取本章 均在位")
    }

    companion object {
        const val BOOK_NAME = "自检缓存不一致书"
        const val CH1 = "第一章 起"
        const val CH2 = "第二章 承"
        const val CH3 = "第三章 转"
        const val CH1_BODY = "缓存还在的正文，可正常阅读。"
        const val URL1 = "https://selftest.invalid/stale/chapter-1.html"
        const val URL2 = "https://selftest.invalid/stale/chapter-2.html"
        const val URL3 = "https://selftest.invalid/stale/chapter-3.html"

        /** 与服务端 cache_key_of 同规则：[^\w\u4e00-\u9fff-] → '_'，取后 60 字符 */
        fun cacheKeyOf(url: String): String =
            Regex("[^\\w\\u4e00-\\u9fff-]").replace(url, "_").takeLast(60)
    }
}
