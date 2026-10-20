package com.webnovel.mobile

import androidx.activity.compose.setContent
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextReplacement
import org.junit.After
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.Assert.*
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.atomic.AtomicInteger

class NovelChapterFinderUiTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()
    private lateinit var server: ServerSocket
    private val gets = AtomicInteger()
    private val writes = AtomicInteger()
    private var selected = 0
    @Before fun guard() {
        val qemu = ProcessBuilder("getprop", "ro.kernel.qemu").start().inputStream
            .bufferedReader().use { it.readText().trim() }
        assertEquals("合成小说章节测试仅限模拟器", "1", qemu)
    }
    @After fun cleanup() { if (::server.isInitialized) server.close() }
    private fun texts(s: String) = rule.onAllNodesWithText(s, substring = true).fetchSemanticsNodes().size
    @Test fun exactIndexFiltersAndEmptyRecoveryDoNotFetchOrMutate() {
        server = ServerSocket(0, 10, InetAddress.getByName("127.0.0.1"))
        val chapters = (1..500).joinToString(",") {
            """{"index":$it,"name":"目录验收第${it}章","downloaded":${it != 419},"failed":${it == 419}}"""
        }
        Thread {
            while (!server.isClosed) try {
                server.accept().use { socket ->
                    val reader = socket.getInputStream().bufferedReader()
                    val request = reader.readLine().orEmpty()
                    while (!reader.readLine().isNullOrEmpty()) { }
                    if (request.startsWith("GET ")) gets.incrementAndGet() else writes.incrementAndGet()
                    val body = (if (request.contains("/progress ")) """{"ok":true,"idx":480}""" else
                        """{"key":"fixture","name":"查找目录验收","author":"隔离作者","total":500,"done":499,"chapters":[$chapters]}""").toByteArray()
                    socket.getOutputStream().apply {
                        write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${body.size}\r\nConnection: close\r\n\r\n".toByteArray())
                        write(body); flush()
                    }
                }
            } catch (_: java.io.IOException) { }
        }.apply { isDaemon = true; start() }
        val gateway = EngineGateway(rule.activity)
        rule.activityRule.scenario.onActivity { activity -> activity.setContent {
            MaterialTheme { BookDetailScreen(gateway, EngineEndpoint("fixture", server.localPort, ""), "fixture", {}, { selected = it }) }
        } }
        rule.waitUntil(10_000) { texts("目录 500 章") > 0 }
        rule.onNodeWithTag("novel_chapter_query").performScrollTo().performTextReplacement("４１８")
        rule.waitUntil(5_000) { texts("显示 1 / 500 章") > 0 }
        rule.onNodeWithText("目录验收第418章").performScrollTo().performClick()
        rule.runOnIdle { assertEquals("不能跳到筛选后第1项或旧进度480", 418, selected) }
        rule.onNodeWithTag("novel_chapter_query").performScrollTo().performTextReplacement("419")
        rule.onNodeWithTag("novel_chapter_filter_downloaded").performClick()
        rule.waitUntil(5_000) { texts("没有匹配的章节") > 0 }
        rule.onNodeWithTag("novel_chapter_filter_missing").performClick()
        rule.waitUntil(5_000) { texts("显示 1 / 500 章") > 0 }
        rule.onNodeWithText("目录验收第419章").performScrollTo()
        rule.onNodeWithTag("novel_chapter_filter_reset").performScrollTo().performClick()
        rule.waitUntil(5_000) { texts("显示 500 / 500 章") > 0 }
        assertEquals("筛选不额外取目录", 2, gets.get())
        assertEquals("筛选和回调不能创建下载/改阅读记录", 0, writes.get())
    }
}
