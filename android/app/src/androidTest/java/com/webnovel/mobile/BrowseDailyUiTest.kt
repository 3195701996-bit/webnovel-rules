package com.webnovel.mobile

import androidx.activity.compose.setContent
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import org.junit.After
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.Assert.*
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.atomic.AtomicInteger

class BrowseDailyUiTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()
    private lateinit var server: ServerSocket
    private val gets = AtomicInteger()
    private val writes = AtomicInteger()
    private val statusGets = AtomicInteger()
    private var opened: Dest? = null
    private var back = false

    @Before fun guard() {
        val qemu = ProcessBuilder("getprop", "ro.kernel.qemu").start().inputStream
            .bufferedReader().use { it.readText().trim() }
        assertEquals("合成浏览测试仅限模拟器", "1", qemu)
    }
    @After fun cleanup() { if (::server.isInitialized) server.close() }
    private fun texts(value: String) = rule.onAllNodesWithText(value, substring = true).fetchSemanticsNodes().size

    private fun open(diagnostics: Boolean, startCode: Int = 202,
                     startBody: String = """{"started":true}""",
                     statusBody: String = """{"status":"done","done":1,"total":1}""") {
        server = ServerSocket(0, 10, InetAddress.getByName("127.0.0.1"))
        Thread {
            while (!server.isClosed) {
                try {
                    server.accept().use { socket ->
                        val input = socket.getInputStream().bufferedReader()
                        val request = input.readLine().orEmpty()
                        while (!input.readLine().isNullOrEmpty()) { }
                        if (request.startsWith("GET ")) gets.incrementAndGet() else writes.incrementAndGet()
                        val isStatus = request.contains("/verify/status")
                        if (isStatus) statusGets.incrementAndGet()
                        val code = if (request.startsWith("POST ")) startCode else 200
                        val body = (if (request.startsWith("POST ")) startBody else if (isStatus) statusBody else
                            """{"sources":[{"key":"fixture","name":"隔离测试源","status":"supported","reason":"测试依赖说明"}]}""").toByteArray()
                        socket.getOutputStream().apply {
                            write("HTTP/1.1 $code Response\r\nContent-Type: application/json\r\nContent-Length: ${body.size}\r\nConnection: close\r\n\r\n".toByteArray())
                            write(body); flush()
                        }
                    }
                } catch (_: java.io.IOException) { }
            }
        }.apply { isDaemon = true; start() }
        val gateway = EngineGateway(rule.activity)
        val endpoint = EngineEndpoint("fixture", server.localPort, "")
        rule.activityRule.scenario.onActivity { activity -> activity.setContent {
            MaterialTheme {
                if (diagnostics) MangaDiagnosticsScreen(gateway, endpoint, { opened = it }, { back = true })
                else BrowseScreen(gateway, endpoint, { opened = it })
            }
        } }
    }

    @Test fun dailyBrowseHasAllFourEntriesWithoutDiagnosticRequests() {
        open(false)
        rule.waitUntil(10_000) { texts("发现漫画") > 0 }
        for ((label, destination) in listOf("搜漫画" to Dest.MangaSearch(), "内置漫画源" to Dest.MangaSources,
            "探索（漫画分类 / 小说榜单）" to Dest.Explore, "搜小说" to Dest.NovelSearch)) {
            rule.onAllNodesWithText(label)[0].performScrollTo().performClick()
            rule.runOnIdle { assertEquals(destination, opened) }
        }
        assertEquals(0, texts("漫画源能力"))
        assertEquals(0, texts("实测下一批漫画源"))
        assertEquals(0, gets.get())
        assertEquals(0, writes.get())
    }
    @Test fun independentDiagnosticsRetainsSourceStatusAndBackWithoutAutoVerification() {
        open(true)
        rule.waitUntil(10_000) { texts("隔离测试源") > 0 }
        assertTrue(texts("实测下一批漫画源") > 0)
        assertTrue(texts("测试依赖说明") > 0)
        assertEquals(1, gets.get())
        assertEquals(0, writes.get())
        rule.onNodeWithTag("manga_diagnostics_back").performClick()
        rule.runOnIdle { assertTrue(back) }
    }

    private fun runCheck() {
        rule.waitUntil(10_000) { texts("隔离测试源") > 0 }
        rule.onAllNodesWithText("实测下一批漫画源")[0].performScrollTo().performClick()
    }
    @Test fun failedStartDoesNotPollAndCanRetry() {
        open(true, startCode = 503)
        runCheck()
        rule.waitUntil(10_000) { texts("启动失败：HTTP 503") > 0 }
        assertEquals(0, statusGets.get())
        runCheck()
        rule.waitUntil(10_000) { writes.get() == 2 && texts("实测下一批漫画源") > 0 }
        assertEquals(0, statusGets.get())
    }
    @Test fun invalidSuccessfulStartDoesNotPoll() {
        open(true, startBody = "{}")
        runCheck()
        rule.waitUntil(10_000) { texts("启动响应无效") > 0 }
        assertEquals(0, statusGets.get())
        assertEquals(0, texts("结果已更新"))
    }
    @Test fun unknownStatusIsNotCompletion() {
        open(true, statusBody = "{}")
        runCheck()
        rule.waitUntil(10_000) { texts("验证进度响应无效") > 0 }
        assertEquals(1, statusGets.get())
        assertEquals(0, texts("结果已更新"))
    }
    @Test fun completedRoundDoesNotClaimAllSourcesWork() {
        open(true)
        runCheck()
        rule.waitUntil(10_000) { texts("各源是否可用请查看实测结论") > 0 }
        assertEquals(1, writes.get())
        assertEquals(1, statusGets.get())
    }
}
