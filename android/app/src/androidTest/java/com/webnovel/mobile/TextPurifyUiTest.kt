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
 * **正文净化 + 已下载内容的重新净化**（用户反馈："导出的正文里有大量广告与无意义分隔符"）。
 *
 * 用合成书（不联网、确定性）验证：
 *   1. 接口：`POST /api/books/<key>/reclean` 先 dry-run 报数字（**不动文件**），
 *      再真净化：删掉广告行/分隔线，**作者正文一字不动**，章节标题保留；
 *   2. 幂等：跑第二遍 0 改动；
 *   3. 界面：详情页有「净化正文」入口，点一下出预检数字 + 二次确认，确认后显示实测结果。
 *
 * 真实样本来自用户导出的《在美漫当心灵导师的日子》：
 *   · `【新章节更新迟缓的问题，在能换源的app上终于有了解决之道，这里下载huanyuanap】`
 *   · `小书亭app`、`言情小说吧免费阅读`
 *   · 纯 `─` 分隔线（该书导出文件里有 5014 行——旧净化清不掉，且导出文件不会自动更新）
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class TextPurifyUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext
    private lateinit var gateway: EngineGateway
    private lateinit var bookDir: File

    private val key = "自检_purify"

    private fun ev(line: String) = println("PURIFY_EVIDENCE $line")

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size
    private fun texts(t: String, substring: Boolean = false) =
        rule.onAllNodesWithText(t, substring = substring).fetchSemanticsNodes().size

    private val adLine = "【新章节更新迟缓的问题，在能换源的app上终于有了解决之道，" +
        "这里下载huanyuanap】"

    @Before
    fun setUp() {
        val booksDir = File(ctx.filesDir, "runtime/books")
        bookDir = File(booksDir, key)
        bookDir.deleteRecursively()
        require(bookDir.mkdirs()) { "无法创建合成书目录：$bookDir" }
        File(bookDir, "_state.json").writeText(
            JSONObject().apply {
                put("book", JSONObject().apply {
                    put("name", BOOK_NAME); put("author", "自检")
                    put("source_uid", "selftest")
                    put("book_url", "https://selftest.invalid/purify/")
                })
                put("chapters", JSONArray().apply {
                    put(JSONObject().put("name", "第一章 干净").put("url", URL1))
                    put(JSONObject().put("name", "第二章 有广告").put("url", URL2))
                })
                put("completed", JSONArray().put(URL1).put(URL2))
                put("failed", JSONObject())
                put("updated_at", "2026-01-01T00:00:00")
            }.toString(), Charsets.UTF_8)
        // 第 1 章：干净（不该被改写）；第 2 章：带广告 + 分隔线 + 作者正文（必须保留）
        File(bookDir, cacheKeyOf(URL1) + ".cache")
            .writeText("第一章 干净\n\n这一章本来就是干净的，净化不该动它。", Charsets.UTF_8)
        File(bookDir, cacheKeyOf(URL2) + ".cache")
            .writeText(
                "第二章 有广告\n\n正文第一段，正常内容。\n" +
                    "─".repeat(30) + "\n" + adLine + "\n小书亭app\n" +
                    "全都给我去下国家反诈APP！！！\n他打开了那个app看了看。\n",
                Charsets.UTF_8)

        gateway = EngineGateway(ctx)
        val st = runBlocking { gateway.connect() }
        assertTrue("引擎未就绪：$st", st is EngineState.Ready)
    }

    @After
    fun tearDown() {
        bookDir.deleteRecursively()
        File(ctx.filesDir, "runtime/trash").listFiles()
            ?.filter { it.name.startsWith(key) }?.forEach { it.deleteRecursively() }
        assertFalse("合成书目录未清理干净", bookDir.exists())
    }

    @Test
    fun api_dryRunThenPurify_keepsAuthorText() = runBlocking {
        val ep = gateway.currentEndpoint()!!
        val p2 = File(bookDir, cacheKeyOf(URL2) + ".cache")
        val before = p2.readText(Charsets.UTF_8)

        // 1) dry-run：报数字、不动文件
        val dry = gateway.httpPost(ep.port, "/api/books/$key/reclean?dry_run=1")
        assertTrue("dry-run 应 200：HTTP ${dry.code}", dry.ok)
        val d = JSONObject(dry.body)
        assertTrue("应标 dry_run", d.optBoolean("dry_run"))
        assertEquals("两章里只有第 2 章需要净化", 1, d.optInt("changed"))
        assertEquals("干净的第 1 章不该被改写", 1, d.optInt("already_clean"))
        assertTrue("必须报告删了几行垃圾：${d.optInt("removed_lines")}", d.optInt("removed_lines") >= 3)
        assertTrue("必须有样例供用户核对", d.optJSONArray("samples")!!.length() > 0)
        assertEquals("dry-run 不许改文件", before, p2.readText(Charsets.UTF_8))
        ev("预检：changed=${d.optInt("changed")} lines=${d.optInt("removed_lines")} " +
            "chars=${d.optInt("removed_chars")} 样例=${d.optJSONArray("samples")}")

        // 2) 真净化
        val run = gateway.httpPost(ep.port, "/api/books/$key/reclean", "{\"dry_run\":false}")
        assertTrue("净化应 200：HTTP ${run.code} ${run.body.take(80)}", run.ok)
        val r = JSONObject(run.body)
        assertFalse("不是 dry-run", r.optBoolean("dry_run"))
        assertEquals(1, r.optInt("changed"))
        val after = p2.readText(Charsets.UTF_8)
        assertFalse("广告行必须删掉", after.contains("huanyuanap"))
        assertFalse("无意义分隔线必须删掉", after.contains("─"))
        assertFalse("App 名广告行必须删掉", after.contains("小书亭"))
        assertTrue("正文段落必须保留", after.contains("正文第一段，正常内容。"))
        assertTrue("作者原话必须保留（含 APP 字样）", after.contains("全都给我去下国家反诈APP！！！"))
        assertTrue("正文里的 app 字样必须保留", after.contains("他打开了那个app看了看。"))
        assertTrue("章节标题必须保留", after.startsWith("第二章 有广告"))
        ev("净化后：${after.length} 字（原 ${before.length} 字）；广告/分隔线已清，作者正文保留")

        // 3) 幂等
        val again = JSONObject(
            gateway.httpPost(ep.port, "/api/books/$key/reclean?dry_run=1").body)
        assertEquals("跑第二遍必须 0 改动", 0, again.optInt("changed"))
        assertEquals(0, again.optInt("removed_lines"))
        ev("幂等：第二遍 changed=0")
        Unit
    }

    @Test
    fun ui_offersPurifyWithPreviewAndConfirm() {
        gateway = EngineGateway(ctx)
        runBlocking { gateway.connect() }
        // 书架 → 合成书 → 详情
        rule.waitUntil(150_000) { texts(BOOK_NAME) > 0 }
        rule.onAllNodesWithText(BOOK_NAME)[0].performScrollTo().performClick()
        rule.waitUntil(60_000) { nodes("reclean_button") > 0 }
        ev("详情页有「净化正文」入口")

        rule.onAllNodesWithTag("reclean_button")[0].performScrollTo().performClick()
        rule.waitUntil(60_000) { texts("净化已下载正文？") > 0 }
        val dialogTexts = texts("将重新净化", substring = true)
        assertTrue("确认框必须给出预检数字（将重新净化 N 章…）", dialogTexts > 0)
        ev("出现二次确认框，含预检数字")
        rule.onAllNodesWithTag("reclean_confirm")[0].performClick()
        rule.waitUntil(60_000) {
            texts("已净化", substring = true) > 0
        }
        val msg = rule.onAllNodesWithText("已净化", substring = true)[0]
            .fetchSemanticsNode().config.toString()
        ev("净化结果提示：${msg.take(160)}")
        // 界面上文案已生效（文件层面由另一个用例断言）
        Unit
    }

    companion object {
        const val BOOK_NAME = "自检净化书"
        const val URL1 = "https://selftest.invalid/purify/chapter-1.html"
        const val URL2 = "https://selftest.invalid/purify/chapter-2.html"

        fun cacheKeyOf(url: String): String =
            Regex("[^\\w\\u4e00-\\u9fff-]").replace(url, "_").takeLast(60)
    }
}
