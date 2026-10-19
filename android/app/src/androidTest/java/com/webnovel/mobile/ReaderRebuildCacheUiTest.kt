package com.webnovel.mobile

import androidx.activity.compose.setContent
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import coil.ImageLoader
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.atomic.AtomicInteger

/** Real Compose and HTTP transport; synthetic loopback responses, no engine/media writes. */
class ReaderRebuildCacheUiTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()
    private lateinit var server: ServerSocket
    private val urls = AtomicInteger()
    private val rebuilds = AtomicInteger()
    private val source = "fixture-rebuild-ui"
    private val comic = "one"

    @Before fun guard() {
        val qemu = ProcessBuilder("getprop", "ro.kernel.qemu").start().inputStream.bufferedReader().use { it.readText().trim() }
        assertEquals("合成阅读器仅限模拟器", "1", qemu)
    }

    @After fun cleanup() {
        if (::server.isInitialized) server.close()
        MangaReadCache.invalidate(source, comic)
    }

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size

    private fun exercise(success: Boolean) {
        server = ServerSocket(0, 10, InetAddress.getByName("127.0.0.1"))
        val port = server.localPort
        val image = SelfTestComic.makeJpeg(1)
        Thread {
            while (!server.isClosed) {
                try {
                    server.accept().use { socket ->
                        val input = socket.getInputStream().bufferedReader()
                        val request = input.readLine().orEmpty()
                        while (!input.readLine().isNullOrEmpty()) { }
                        var code = 200
                        val body = when {
                            request.contains("/rebuild-images?") -> {
                                rebuilds.incrementAndGet()
                                if (!success) code = 500
                                (if (success) "{\"ok\":true}" else "{\"error\":\"fixture failure\"}").toByteArray()
                            }
                            request.contains("/urls?") -> {
                                urls.incrementAndGet()
                                """{"count":1,"local_only":true,"images":[{"url":"/image","local":true}]}""".toByteArray()
                            }
                            request.contains(" /image ") -> image
                            else -> "{\"ok\":true}".toByteArray()
                        }
                        val type = if (request.contains(" /image ")) "image/jpeg" else "application/json"
                        socket.getOutputStream().apply {
                            write("HTTP/1.1 $code Result\r\nContent-Type: $type\r\nContent-Length: ${body.size}\r\nConnection: close\r\n\r\n".toByteArray())
                            write(body); flush()
                        }
                    }
                } catch (_: java.io.IOException) { }
            }
        }.apply { isDaemon = true; start() }
        val detail = EngineData.mangaDetail("""{"source":"$source","comic_id":"$comic","title":"隔离重建","chapters":[{"id":"ch1","name":"第1话"}],"downloaded":["ch1"],"local_only":true}""")!!
        val stale = MangaChapterPages(1, true, listOf("http://127.0.0.1:$port/image"), staleProcessing = true)
        MangaReadCache.putDetail(source, comic, detail, true)
        MangaReadCache.putUrls(source, comic, "ch1", stale, true)
        val gateway = EngineGateway(rule.activity)
        val loader = ImageLoader(rule.activity)
        rule.activityRule.scenario.onActivity { activity -> activity.setContent {
            MaterialTheme {
                MangaReaderScreen(gateway, EngineEndpoint("fixture", port, ""), source, comic, 0, 1, loader, {}, localCatalog = true)
            }
        } }
        rule.waitUntil(10_000) { nodes("stale_cache_banner") > 0 }
        assertEquals(0, urls.get()) // The initial banner must also work on a shared-cache hit.
        rule.onNodeWithTag("stale_cache_rebuild").performClick()
        rule.waitUntil(10_000) { rebuilds.get() == 1 }
        if (success) {
            rule.waitUntil(10_000) { urls.get() == 1 && nodes("stale_cache_banner") == 0 }
            assertFalse(MangaReadCache.getUrls(source, comic, "ch1", true)!!.staleProcessing)
        } else {
            rule.waitUntil(10_000) {
                rule.onAllNodesWithText("重新获取失败：HTTP 500", substring = true).fetchSemanticsNodes().isNotEmpty()
            }
            assertEquals(0, urls.get())
            assertEquals(stale, MangaReadCache.getUrls(source, comic, "ch1", true))
        }
    }

    @Test fun successRefetchesUrlsInsteadOfReusingOldSharedCache() = exercise(true)
    @Test fun failurePreservesSharedChapterCache() = exercise(false)
}
