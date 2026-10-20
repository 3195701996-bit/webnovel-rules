package com.webnovel.mobile

import androidx.activity.compose.setContent
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import coil.ImageLoader
import org.junit.After
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.Assert.*
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** Controlled old HTTP response after category navigation; isolated emulator only. */
class ExploreStaleResponseUiTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()
    private lateinit var server: ServerSocket
    private val oldStarted = CountDownLatch(1)
    private val releaseOld = CountDownLatch(1)
    private val oldSent = CountDownLatch(1)
    private val oldCalls = AtomicInteger()
    private val parentBacks = AtomicInteger()
    private lateinit var loader: ImageLoader

    @Before fun guard() {
        val qemu = ProcessBuilder("getprop", "ro.kernel.qemu").start().inputStream
            .bufferedReader().use { it.readText().trim() }
        assertEquals("延迟榜单测试仅限模拟器", "1", qemu)
    }
    @After fun cleanup() {
        releaseOld.countDown()
        if (::server.isInitialized) server.close()
        if (::loader.isInitialized) loader.shutdown()
    }
    private fun texts(value: String) = rule.onAllNodesWithText(value, substring = true).fetchSemanticsNodes().size
    private fun open(oldFails: Boolean, sameCategory: Boolean = false, systemBack: Boolean = false,
                     manga: Boolean = false) {
        server = ServerSocket(0, 10, InetAddress.getByName("127.0.0.1"))
        Thread {
            while (!server.isClosed) {
                try {
                    val socket = server.accept()
                    Thread {
                        try { socket.use {
                            val input = socket.getInputStream().bufferedReader()
                            val request = input.readLine().orEmpty()
                            while (!input.readLine().isNullOrEmpty()) { }
                            val old = request.contains("url=%2Fold") && oldCalls.incrementAndGet() == 1
                            if (old) { oldStarted.countDown(); check(releaseOld.await(10, TimeUnit.SECONDS)) }
                            val body = (when {
                                manga && request.contains("/sources ") -> """{"sources":[],"manga":{"sources":[{"key":"mangadex","name":"隔离漫画源","categories":[{"name":"漫画隔离分类","key":"fixture","group":"题材"}]}]}}"""
                                manga -> """{"results":[{"id":"fixture","title":"漫画结果","source":"mangadex","source_name":"隔离漫画源"}],"total":1,"total_pages":1,"has_more":false}"""
                                request.contains("/sources ") -> """{"sources":[{"uid":"fixture","name":"隔离榜单源","categories":[{"title":"旧榜单","url":"/old"},{"title":"新榜单","url":"/new"}]}]}"""
                                old && oldFails -> """{"error":"过期失败说明"}"""
                                else -> """{"books":[{"name":"${if (old) "旧作品" else "新作品"}","book_url":"/book","source_uid":"fixture"}]}"""
                            }).toByteArray()
                            val code = if (old && oldFails) 502 else 200
                            socket.getOutputStream().apply {
                                write("HTTP/1.1 $code Result\r\nContent-Type: application/json\r\nContent-Length: ${body.size}\r\nConnection: close\r\n\r\n".toByteArray())
                                write(body); flush()
                            }
                            if (old) oldSent.countDown()
                        } } catch (_: Exception) { }
                    }.apply { isDaemon = true; start() }
                } catch (_: java.io.IOException) { }
            }
        }.apply { isDaemon = true; start() }
        val gateway = EngineGateway(rule.activity)
        loader = ImageLoader.Builder(rule.activity).build()
        rule.activityRule.scenario.onActivity { activity -> activity.setContent {
            MaterialTheme {
                androidx.activity.compose.BackHandler { parentBacks.incrementAndGet() }
                ExploreScreen(gateway, EngineEndpoint("fixture", server.localPort, ""), loader, {},
                    { parentBacks.incrementAndGet() })
            }
        } }
        if (manga) {
            rule.waitUntil(10_000) { texts("漫画隔离分类") > 0 }
            rule.onNodeWithText("漫画隔离分类").performClick()
            rule.waitUntil(10_000) { texts("漫画结果") > 0 }
            rule.activityRule.scenario.onActivity { it.onBackPressedDispatcher.onBackPressed() }
            rule.waitUntil(10_000) { texts("漫画结果") == 0 && texts("漫画隔离分类") > 0 }
            assertEquals("系统返回不能越过漫画分类层级", 0, parentBacks.get())
            rule.onNodeWithText("漫画隔离分类").performClick()
            rule.waitUntil(10_000) { texts("漫画结果") > 0 }
            rule.onNodeWithContentDescription("返回").performClick()
            rule.waitUntil(10_000) { texts("漫画结果") == 0 && texts("漫画隔离分类") > 0 }
            rule.activityRule.scenario.onActivity { it.onBackPressedDispatcher.onBackPressed() }
            assertEquals("根菜单才交给父导航返回", 1, parentBacks.get())
            return
        }
        rule.waitUntil(10_000) { texts("隔离榜单源") > 0 }
        rule.onNodeWithText("隔离榜单源").performClick()
        rule.onNodeWithText("旧榜单").performClick()
        assertTrue(oldStarted.await(5, TimeUnit.SECONDS))
        if (systemBack) {
            rule.activityRule.scenario.onActivity { it.onBackPressedDispatcher.onBackPressed() }
            rule.waitUntil(10_000) { texts("旧榜单") > 0 && texts("新榜单") > 0 }
        } else rule.onNodeWithContentDescription("返回").performClick()
        rule.onNodeWithText(if (sameCategory) "旧榜单" else "新榜单").performClick()
        rule.waitUntil(10_000) { texts("新作品") > 0 }
        releaseOld.countDown()
        assertTrue(oldSent.await(5, TimeUnit.SECONDS))
        // HTTP completion is external to Compose idling; observe a bounded window
        // after the stale response is sent, rather than assert before it can land.
        android.os.SystemClock.sleep(600)
        rule.waitForIdle()
        assertEquals(1, texts("新作品"))
        assertEquals(0, texts("旧作品"))
        assertEquals(0, texts("读取榜单失败"))
        assertEquals(0, texts("过期失败说明"))
        if (systemBack) {
            rule.activityRule.scenario.onActivity { it.onBackPressedDispatcher.onBackPressed() }
            rule.waitUntil(10_000) { texts("旧榜单") > 0 && texts("新榜单") > 0 }
            rule.activityRule.scenario.onActivity { it.onBackPressedDispatcher.onBackPressed() }
            rule.waitUntil(10_000) { texts("旧榜单") == 0 && texts("隔离榜单源") > 0 }
            assertEquals("榜单结果和源菜单逐层返回", 0, parentBacks.get())
        }
    }
    @Test fun oldSuccessCannotReplaceNewCategory() = open(false)
    @Test fun oldFailureCannotHideNewCategory() = open(true)
    @Test fun reenteringSameCategoryStillRejectsPreviousVisitResponse() = open(false, sameCategory = true)
    @Test fun systemBackPopsNestedLevelsAndRejectsStaleSuccess() = open(false, systemBack = true)
    @Test fun systemBackRejectsStaleFailureOnReenteredCategory() = open(true, sameCategory = true, systemBack = true)
    @Test fun mangaSystemBackMatchesToolbarAndOnlyRootEscapes() = open(false, manga = true)
}
