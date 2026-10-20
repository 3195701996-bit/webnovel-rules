package com.webnovel.mobile

import androidx.activity.compose.setContent
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextReplacement
import androidx.compose.ui.test.performScrollTo
import org.junit.After
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.Assert.*
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.atomic.AtomicInteger

/** Real UI and HTTP, synthetic loopback only; never touches user downloads. */
class LibraryUpdateFeedbackUiTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()
    private lateinit var server: ServerSocket
    private val polls = AtomicInteger()
    private val mutations = AtomicInteger()

    @Before fun guard() {
        val qemu = ProcessBuilder("getprop", "ro.kernel.qemu").start().inputStream
            .bufferedReader().use { it.readText().trim() }
        assertEquals("合成下载反馈测试仅限模拟器", "1", qemu)
    }
    @After fun cleanup() { if (::server.isInitialized) server.close() }
    private fun texts(value: String) = rule.onAllNodesWithText(value, substring = true).fetchSemanticsNodes().size

    private fun open(startCode: Int, statusCode: Int, status: String, startCheck: Boolean = true,
                     tasks: String = "{\"tasks\":[]}") {
        server = ServerSocket(0, 10, InetAddress.getByName("127.0.0.1"))
        Thread {
            while (!server.isClosed) {
                try {
                    server.accept().use { socket ->
                        val input = socket.getInputStream().bufferedReader()
                        val request = input.readLine().orEmpty()
                        if (request.startsWith("POST ") || request.startsWith("DELETE ")) mutations.incrementAndGet()
                        while (!input.readLine().isNullOrEmpty()) { }
                        val statusRequest = request.contains("/check-updates/status")
                        val startRequest = request.contains("POST /api/manga/library/check-updates ")
                        val code = if (statusRequest) statusCode else if (startRequest) startCode else 200
                        val body = when {
                            statusRequest -> { polls.incrementAndGet(); status }
                            startRequest -> "{\"ok\":${startCode == 200}}"
                            else -> tasks
                        }.toByteArray()
                        socket.getOutputStream().apply {
                            write("HTTP/1.1 $code Result\r\nContent-Type: application/json\r\nContent-Length: ${body.size}\r\nConnection: close\r\n\r\n".toByteArray())
                            write(body); flush()
                        }
                    }
                } catch (_: java.io.IOException) { }
            }
        }.apply { isDaemon = true; start() }
        val gateway = EngineGateway(rule.activity)
        rule.activityRule.scenario.onActivity { activity -> activity.setContent {
            MaterialTheme { DownloadsScreen(gateway, EngineEndpoint("fixture", server.localPort, ""), {}) }
        } }
        if (startCheck) rule.onNodeWithTag("check_library_update").performScrollTo().performClick()
    }

    @Test fun searchAndStateFiltersDoNotMutateFiftyTasks() {
        val items = (1..50).joinToString(",") { index ->
            """{"id":"manga_fixture-$index","type":"manga","title":"查找作品$index","status":"${if (index == 42) "error" else "done"}","running":false,"progress":{"done":1,"total":1},"manga_key":"copymanga:fixture-$index"}"""
        }
        open(200, 200, "{}", startCheck = false, tasks = "{\"tasks\":[$items]}")
        rule.waitUntil(10_000) { texts("显示 50 / 50 项") > 0 }
        rule.onNodeWithTag("download_filter_failed").performClick()
        rule.waitUntil(5_000) { texts("显示 1 / 50 项") > 0 }
        rule.onNodeWithTag("download_filter_all").performClick()
        rule.onNodeWithTag("download_task_query").performTextReplacement("作品４２")
        rule.waitUntil(5_000) { texts("显示 1 / 50 项") > 0 }
        rule.onNodeWithTag("download_task_query").performTextReplacement("无匹配作品")
        rule.waitUntil(5_000) { texts("没有匹配的任务") > 0 }
        rule.onAllNodesWithText("清除筛选")[0].performScrollTo().performClick()
        rule.waitUntil(5_000) { texts("显示 50 / 50 项") > 0 }
        assertEquals(0, mutations.get())
    }

    @Test fun failedStartDoesNotPollOrInventCompletion() {
        open(500, 200, "{}")
        rule.waitUntil(10_000) { texts("启动检查失败：HTTP 500") > 0 }
        android.os.SystemClock.sleep(2500)
        assertEquals(0, polls.get())
        assertEquals(0, texts("全部已是最新"))
    }
    @Test fun statusFailureRemainsRetryableNotUpToDate() {
        open(200, 502, "{}")
        rule.waitUntil(10_000) { texts("读取检查进度失败：HTTP 502") > 0 }
        assertEquals(1, polls.get())
        assertEquals(0, texts("全部已是最新"))
        assertTrue(texts("检查书库更新") > 0)
    }
    @Test fun malformedStatusIsNotCompletion() {
        open(200, 200, "{}")
        rule.waitUntil(10_000) { texts("检查进度响应无效") > 0 }
        assertEquals(0, texts("全部已是最新"))
    }
    @Test fun partialFailureSummaryAppearsOnceWithoutCallingMissingChaptersNew() {
        open(200, 200, """{"running":false,"done":2,"total":2,"results":[{"title":"旧缺章","ok":true,"missing_count":1,"missing":["old"]},{"title":"失败","ok":false,"error":"离线"}]}""")
        rule.waitUntil(10_000) { texts("1 部有待下载内容，1 部检查失败") > 0 }
        assertEquals(1, texts("1 部有待下载内容，1 部检查失败"))
        assertEquals(0, texts("有新话"))
        assertEquals(0, texts("全部已是最新"))
    }
}
