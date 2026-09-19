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
 * **存储管理（原生）验收**：路线 §7 P1-2 —— "按源/作品/章节查看占用，选择性清除错误缓存"。
 *
 * 用合成书目（不联网、确定性）验证三件事：
 *   1. 占用明细是真的：`GET /api/storage` 里能按书看到已缓存章节数与字节数；
 *   2. **选择性**清理：只清"错误缓存"（抓取失败的章节 + 内容为空的坏缓存），
 *      正常的已下载章节**一张都不能少**；
 *   3. 界面上能走通：设置 → 存储管理 → 看到总占用与逐书明细 → 二次确认后清理，
 *      并显示服务端实测的释放量。
 *
 * 清理纪律：合成书目录用完即删（含 runtime/trash 里本轮产生的条目）。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class StorageUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext
    private lateinit var gateway: EngineGateway
    private lateinit var bookDir: File

    private val key = "自检_storage"

    private fun ev(line: String) = println("STORAGE_EVIDENCE $line")

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size
    private fun texts(t: String, substring: Boolean = false) =
        rule.onAllNodesWithText(t, substring = substring).fetchSemanticsNodes().size

    @Before
    fun setUp() {
        val booksDir = File(ctx.filesDir, "runtime/books")
        bookDir = File(booksDir, key)
        bookDir.deleteRecursively()
        require(bookDir.mkdirs()) { "无法创建合成书目录：$bookDir" }
        // 第 1 章：正常已下载；第 2 章：抓取失败（failed 有记录）；第 3 章：缓存文件是空的（坏缓存）
        File(bookDir, "_state.json").writeText(
            JSONObject().apply {
                put("book", JSONObject().apply {
                    put("name", BOOK_NAME); put("author", "自检")
                    put("source_uid", "selftest")
                    put("book_url", "https://selftest.invalid/storage/")
                })
                put("chapters", JSONArray().apply {
                    put(JSONObject().put("name", "第一章").put("url", URL1))
                    put(JSONObject().put("name", "第二章").put("url", URL2))
                    put(JSONObject().put("name", "第三章").put("url", URL3))
                })
                put("completed", JSONArray().put(URL1).put(URL3))
                put("failed", JSONObject().put(URL2, "自检：模拟抓取失败"))
                put("updated_at", "2026-01-01T00:00:00")
            }.toString(), Charsets.UTF_8)
        File(bookDir, cacheKeyOf(URL1) + ".cache")
            .writeText("第一章\n\n正常已下载的正文，长度足够。", Charsets.UTF_8)
        File(bookDir, cacheKeyOf(URL3) + ".cache").writeText("", Charsets.UTF_8)

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
    fun api_reportsPerBookUsageAndSelectiveClear() = runBlocking {
        val ep = gateway.currentEndpoint()!!

        // 1) 占用明细：这本书必须出现，且缓存数与失败数与磁盘一致
        val u = JSONObject(gateway.httpText(ep.port, "/api/storage").body)
        assertTrue("总占用应为正数：${u.optLong("total_bytes")}", u.optLong("total_bytes") > 0)
        val books = u.optJSONArray("novel_books")!!
        var mine: JSONObject? = null
        for (i in 0 until books.length()) {
            if (books.optJSONObject(i)?.optString("key") == key) mine = books.optJSONObject(i)
        }
        assertNotNull("存储明细里应能看到合成书", mine)
        val b = mine!!
        assertEquals("总章数", 3, b.optInt("total"))
        // 第 1、3 章有缓存文件（第 3 章是空文件，也算"占用"），第 2 章没有
        assertEquals("已缓存章节数", 2, b.optInt("cached"))
        assertEquals("失败章节数", 1, b.optInt("failed"))
        assertTrue("应有字节数（含空文件与 state）", b.optLong("bytes") > 0)
        ev("占用明细：${b.optString("name")} 已缓存 ${b.optInt("cached")}/${b.optInt("total")}，" +
            "失败 ${b.optInt("failed")}，${b.optLong("bytes")} 字节")

        // 2) 选择性清"错误缓存"：空缓存被删、失败记录被清、正常章节一张不少
        val r = gateway.httpPost(ep.port, "/api/storage/clear",
            JSONObject().put("scope", "book_chapters").put("key", key)
                .put("only", "failed").toString())
        assertTrue("清理应 200：HTTP ${r.code} ${r.body.take(80)}", r.ok)
        val res = JSONObject(r.body)
        assertTrue("应 ok=true", res.optBoolean("ok"))
        assertFalse("坏缓存（空文件）必须被删掉",
            File(bookDir, cacheKeyOf(URL3) + ".cache").exists())
        assertTrue("正常的已下载章节不得被删",
            File(bookDir, cacheKeyOf(URL1) + ".cache").isFile)
        val state = JSONObject(File(bookDir, "_state.json").readText(Charsets.UTF_8))
        assertEquals("状态里 completed 应剔除被删章节", 1,
            state.optJSONArray("completed")!!.length())
        assertEquals("失败记录应被清掉", 0, state.optJSONObject("failed")!!.length())
        ev("错误缓存清理：释放 ${res.optLong("freed_bytes")} 字节；正常章节保留，" +
            "completed=${state.optJSONArray("completed")!!.length()} failed=0")

        // 3) 占用随之下降（同一个接口再读一次，数字必须变化）
        val u2 = JSONObject(gateway.httpText(ep.port, "/api/storage").body)
        var mine2: JSONObject? = null
        for (i in 0 until u2.optJSONArray("novel_books")!!.length()) {
            val o = u2.optJSONArray("novel_books")!!.optJSONObject(i)
            if (o?.optString("key") == key) mine2 = o
        }
        assertEquals("清理后已缓存章节数", 1, mine2!!.optInt("cached"))

        // 4) 拒绝类请求：不给 only/chapters 时不得动手
        val bad = gateway.httpPost(ep.port, "/api/storage/clear",
            JSONObject().put("scope", "book_chapters").put("key", key).toString())
        assertEquals("缺少 only/chapters 应 400", 400, bad.code)
        assertTrue("正常章节仍在（拒绝请求不得删东西）",
            File(bookDir, cacheKeyOf(URL1) + ".cache").isFile)
        ev("参数校验：缺 only/chapters → HTTP ${bad.code}，正常章节未受影响")
        Unit
    }

    @Test
    fun ui_showsUsageAndClearsWithConfirmation() {
        // 设置 → 存储管理
        rule.waitUntil(150_000) { texts("设置") > 0 }
        rule.onAllNodesWithText("设置")[0].performClick()
        rule.waitUntil(60_000) { texts("存储管理") > 0 }
        rule.onAllNodesWithText("存储管理")[0].performScrollTo().performClick()

        // 总占用 + 合成书逐书明细都在界面上
        rule.waitUntil(60_000) { nodes("storage_total") > 0 }
        val totalNode = rule.onAllNodesWithTag("storage_total")[0]
            .fetchSemanticsNode().config.toString()
        ev("界面总占用：$totalNode")
        assertTrue("总占用应显示可读大小（GB/MB/KB/字节）", totalNode.contains("MB") ||
            totalNode.contains("KB") || totalNode.contains("GB") || totalNode.contains("字节"))
        rule.waitUntil(60_000) { texts(BOOK_NAME) > 0 }
        ev("逐书明细里出现《$BOOK_NAME》")

        // 点"清理错误缓存" → 二次确认 → 执行 → 显示实测释放量
        val btn = "clear_failed_" + key
        rule.waitUntil(30_000) { nodes(btn) > 0 }
        rule.onAllNodesWithTag(btn)[0].performScrollTo().performClick()
        rule.waitUntil(30_000) { nodes("storage_confirm_dialog") > 0 }
        ev("出现二次确认对话框")
        // 按 tag 点确认按钮：列表里还有别的「清理」按钮，按文本点会点错
        rule.onAllNodesWithTag("storage_confirm")[0].performClick()
        rule.waitUntil(60_000) { nodes("storage_msg") > 0 }
        val msg = rule.onAllNodesWithTag("storage_msg")[0]
            .fetchSemanticsNode().config.toString()
        ev("清理结果：$msg")
        // 必须显示"清了几项 + 实测释放量"；0 字节也照实说（空缓存本来就不占空间）
        assertTrue("必须显示服务端实测结果（清理项数/释放量/失败原因）：$msg",
            (msg.contains("已清理") && msg.contains("释放")) || msg.contains("清理失败"))
        assertFalse("坏缓存应已被清掉（UI 操作同样生效）",
            File(bookDir, cacheKeyOf(URL3) + ".cache").exists())
        assertTrue("正常章节必须留下",
            File(bookDir, cacheKeyOf(URL1) + ".cache").isFile)
    }

    companion object {
        const val BOOK_NAME = "自检存储书"
        const val URL1 = "https://selftest.invalid/storage/chapter-1.html"
        const val URL2 = "https://selftest.invalid/storage/chapter-2.html"
        const val URL3 = "https://selftest.invalid/storage/chapter-3.html"

        /** 与服务端 cache_key_of 同规则 */
        fun cacheKeyOf(url: String): String =
            Regex("[^\\w\\u4e00-\\u9fff-]").replace(url, "_").takeLast(60)
    }
}
