package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.io.File
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class NovelDownloadBadgeUiTest {
    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    @Test
    fun roundedHundredPercentDoesNotMeanCompleteOrFinished() = runBlocking {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val suffix = System.nanoTime().toString()
        val books = File(context.filesDir, "runtime/books")
        val partialKey = "selftest_badge_partial_$suffix"
        val fullKey = "selftest_badge_full_$suffix"
        val directories = listOf(File(books, partialKey), File(books, fullKey))
        try {
            for ((index, directory) in directories.withIndex()) {
                check(directory.mkdirs())
                val total = if (index == 0) 5035 else 1
                val done = if (index == 0) 5034 else 1
                val chapters = JSONArray()
                val completed = JSONArray()
                for (chapter in 1..total) {
                    val url = "https://selftest.invalid/$suffix/$index/$chapter"
                    chapters.put(JSONObject().put("name", "第${chapter}章").put("url", url))
                    if (chapter <= done) {
                        completed.put(url)
                        File(directory, SelfTestBook.cacheKeyOf(url) + ".cache")
                            .writeText("离线状态自检正文。")
                    }
                }
                File(directory, "_state.json").writeText(JSONObject()
                    .put("book", JSONObject().put("name", if (index == 0) "缺一章状态自检" else "全部下载状态自检"))
                    .put("chapters", chapters).put("completed", completed)
                    .put("failed", JSONObject()).toString())
            }
            val gateway = EngineGateway(context)
            val endpoint = (gateway.connect() as? EngineState.Ready)?.endpoint ?: error("引擎未就绪")
            val response = gateway.httpText(endpoint.port, "/api/books")
            assertTrue(response.ok)
            val partial = EngineData.novels(response.body).single { it.key == partialKey }
            assertEquals(100, partial.percent)
            assertEquals(5034, partial.done)
            rule.openCachedShelf()
            rule.waitUntil(30_000) {
                rule.onAllNodesWithText("已下载全部").fetchSemanticsNodes().isNotEmpty() &&
                    rule.onAllNodesWithText("部分下载").fetchSemanticsNodes().isNotEmpty()
            }
            rule.onNodeWithText("已下载全部").assertIsDisplayed()
            rule.onNodeWithText("部分下载").assertIsDisplayed()
            assertTrue(rule.onAllNodesWithText("完结").fetchSemanticsNodes().isEmpty())
            println("NOVEL_BADGE_UI PASS 5034/5035 percent=100 shows partial; full book shows downloaded-all")
        } finally {
            directories.forEach { directory ->
                directory.deleteRecursively()
                assertTrue("隔离状态书未清理", !directory.exists())
            }
        }
    }
}
