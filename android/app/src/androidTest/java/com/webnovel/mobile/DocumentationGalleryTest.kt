package com.webnovel.mobile

import android.graphics.Bitmap
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.test.platform.app.InstrumentationRegistry
import java.io.File
import java.util.UUID
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.Assume.assumeTrue

/** Opt-in production navigation captures; synthetic data is forbidden on phones. */
class DocumentationGalleryTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()
    private lateinit var book: SelfTestBook

    @Before fun prepare() {
        assertEquals("Documentation fixtures are emulator-only", "1",
            Shell.run("getprop ro.kernel.qemu").trim())
        assumeTrue("Explicit gallery opt-in required",
            InstrumentationRegistry.getArguments().getString("docsGallery") == "1")
        val key = "__docs_gallery_" + UUID.randomUUID().toString().replace("-", "")
        book = SelfTestBook(key)
        book.create()
        val runtime = File(rule.activity.filesDir, "runtime")
        val directory = File(runtime, "books/$key")
        val stateFile = File(directory, "_state.json")
        val state = JSONObject(stateFile.readText())
        state.getJSONObject("book").apply {
            put("name", "星海航行手记")
            put("author", "演示作者")
            put("intro", "驶向星海，记录每一次出发。本书目为界面展示专用的虚构演示数据，不包含真实用户内容。")
        }
        val chapters = JSONArray()
        val completed = JSONArray()
        for (index in 1..120) {
            val name = "第${index}章 星海航行"
            val url = "https://example.invalid/chapter/$index"
            chapters.put(JSONObject().put("name", name).put("url", url))
            if (index <= 80) {
                completed.put(url)
                File(directory, SelfTestBook.cacheKeyOf(url) + ".cache").writeText(
                    name + "\n这是专门用于项目界面展示的虚构演示正文，不对应任何真实作品或用户阅读数据。\n我们在星光下出发，记录沿途的风景。")
            }
        }
        state.put("chapters", chapters).put("completed", completed).put("failed", JSONObject())
        stateFile.writeText(state.toString())
        val progressFile = File(runtime, "book_progress.json")
        val progress = if (progressFile.exists()) JSONObject(progressFile.readText()) else JSONObject()
        progress.put(key, JSONObject().put("idx", 42).put("pct", 37).put("name", "第42章 星海航行"))
        progressFile.writeText(progress.toString())
        rule.activityRule.scenario.recreate()
    }

    @After fun cleanup() {
        if (::book.isInitialized) book.cleanup()
    }

    private fun waitText(value: String) {
        rule.waitUntil(60_000) { rule.onAllNodesWithText(value).fetchSemanticsNodes().isNotEmpty() }
    }

    private fun capture(name: String) {
        rule.waitForIdle()
        // Capture the rendered Compose root, not a stale window transition frame.
        val screenshot = rule.onRoot().captureToImage().asAndroidBitmap()
        val ctx = InstrumentationRegistry.getInstrumentation().targetContext
        val directory = File(checkNotNull(ctx.getExternalFilesDir(null)), "readme-gallery")
        check(directory.mkdirs() || directory.isDirectory)
        File(directory, name).outputStream().use {
            check(screenshot.compress(Bitmap.CompressFormat.PNG, 100, it))
        }
    }

    @Test fun captureProductionBrowseAndNovelDetails() {
        waitText("浏览")
        rule.onAllNodesWithText("浏览")[0].performClick()
        waitText("发现漫画")
        capture("v3-android-browse.png")
        rule.openCachedShelf()
        waitText("星海航行手记")
        rule.onAllNodesWithText("星海航行手记")[0].performClick()
        waitText("目录 120 章 · 已下载 80 章")
        waitText("继续阅读 第 42 章")
        capture("v3-android-novel.png")
    }
}
