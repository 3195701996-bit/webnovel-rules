package com.webnovel.mobile

import androidx.activity.compose.setContent
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import coil.ImageLoader
import org.junit.After
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.Assert.*
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/** Reproduces a completed download leaving a stale missing-chapter button behind. */
class MangaUpdatePreviewLifecycleUiTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()
    private lateinit var server: ServerSocket
    private val downloaded = AtomicBoolean()
    private val submissions = AtomicInteger()
    private val source = "fixture-preview-lifecycle"

    @Before fun guard() {
        val qemu = ProcessBuilder("getprop", "ro.kernel.qemu").start().inputStream
            .bufferedReader().use { it.readText().trim() }
        assertEquals("合成详情下载测试仅限模拟器", "1", qemu)
    }
    @After fun cleanup() {
        if (::server.isInitialized) server.close()
        MangaReadCache.invalidate(source, "one")
    }
    private fun texts(value: String) = rule.onAllNodesWithText(value, substring = true).fetchSemanticsNodes().size

    @Test fun acceptedDownloadInvalidatesPreviewAndCompletionRefreshesDownloadedState() {
        server = ServerSocket(0, 10, InetAddress.getByName("127.0.0.1"))
        Thread {
            while (!server.isClosed) {
                try {
                    server.accept().use { socket ->
                        val input = socket.getInputStream().bufferedReader()
                        val request = input.readLine().orEmpty()
                        while (!input.readLine().isNullOrEmpty()) { }
                        val body = when {
                            request.contains("/check-update ") -> """{"ok":true,"has_update":true,"latest":"第138话","missing_count":1,"missing":[{"id":"new138","name":"第138话"}]}"""
                            request.contains("POST /api/manga/$source/one/download ") -> {
                                submissions.incrementAndGet(); downloaded.set(true)
                                "{\"ok\":true,\"favorited\":true}"
                            }
                            request.contains("/download/status?") -> if (downloaded.get())
                                "{\"status\":\"done\",\"done\":1,\"total\":1}" else "{\"status\":\"idle\"}"
                            request.contains(" /api/manga/$source/one ") ->
                                """{"source":"$source","comic_id":"one","title":"预览生命周期测试","chapters":[{"id":"new138","name":"第138话"}],"downloaded":${if (downloaded.get()) "[\"new138\"]" else "[]"}}"""
                            else -> "{\"items\":[],\"favorites\":[],\"history\":[]}"
                        }.toByteArray()
                        socket.getOutputStream().apply {
                            write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${body.size}\r\nConnection: close\r\n\r\n".toByteArray())
                            write(body); flush()
                        }
                    }
                } catch (_: java.io.IOException) { }
            }
        }.apply { isDaemon = true; start() }
        val gateway = EngineGateway(rule.activity)
        val loader = ImageLoader(rule.activity)
        rule.activityRule.scenario.onActivity { activity -> activity.setContent {
            MaterialTheme {
                MangaDetailScreen(gateway, EngineEndpoint("fixture", server.localPort, ""), source, "one", loader,
                    onBack = {}, onRead = { _, _, _, _, _, _ -> }, onSearchTag = {})
            }
        } }
        rule.waitUntil(10_000) { texts("检查更新") > 0 }
        rule.onAllNodesWithText("检查更新")[0].performScrollTo().performClick()
        rule.waitUntil(10_000) { texts("下载缺失章节（1）") > 0 }
        rule.onAllNodesWithText("下载缺失章节（1）")[0].performScrollTo().performClick()
        rule.waitUntil(10_000) { submissions.get() == 1 && texts("已下载 1") > 0 }
        assertEquals(0, texts("下载缺失章节（1）"))
        assertEquals(0, texts("有 1 个章节未下载"))
        assertEquals(1, submissions.get())
    }
}
