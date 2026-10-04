package com.webnovel.mobile

import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.TimeUnit

/** End-to-end scale regression for opening a large local-only comic catalog. */
@RunWith(AndroidJUnit4::class)
@LargeTest
class MangaDetailScaleTest {
    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private lateinit var comic: SelfTestComic

    @Before
    fun setUp() {
        comic = SelfTestComic(chapterCount = 1_000)
        comic.create()
    }

    @After
    fun tearDown() {
        comic.cleanup()
        assertTrue("长目录自检漫画未清理", !comic.exists())
        assertTrue("长目录自检条目仍留在书库", comic.libraryEntryCount() == 0)
        assertTrue("长目录自检历史仍留存", comic.historyEntryCount() == 0)
    }

    private fun waitForDetail(): Long {
        val startedAt = android.os.SystemClock.elapsedRealtime()
        rule.waitUntil(60_000) {
            rule.onAllNodesWithText("共 1000 单元", substring = true)
                .fetchSemanticsNodes().isNotEmpty()
        }
        rule.onNodeWithTag("manga_detail_list").assertExists()
        return android.os.SystemClock.elapsedRealtime() - startedAt
    }

    private fun openFromCachedShelf(): Long {
        rule.openCachedShelf()
        val tag = "manga_cached:${comic.sourceKey}:${comic.comicIdValue}"
        rule.waitUntil(60_000) {
            rule.onAllNodesWithTag(tag).fetchSemanticsNodes().isNotEmpty()
        }
        rule.onNodeWithTag(tag).performClick()
        return waitForDetail()
    }

    @Test
    fun thousandChapterLocalCatalogOpensAndCanBeReopened() {
        val firstMs = openFromCachedShelf()
        println("MANGA_DETAIL_SCALE chapters=1000 first_open_ms=$firstMs " +
            "device=${android.os.Build.MODEL} api=${android.os.Build.VERSION.SDK_INT}")

        rule.onNodeWithContentDescription("返回").performClick()
        rule.waitUntil(30_000) {
            rule.onAllNodesWithText("已缓存").fetchSemanticsNodes().isNotEmpty()
        }
        val repeatedMs = openFromCachedShelf()
        println("MANGA_DETAIL_SCALE chapters=1000 repeated_open_ms=$repeatedMs " +
            "device=${android.os.Build.MODEL} api=${android.os.Build.VERSION.SDK_INT}")

        assertTrue("长目录首次打开超过 15 秒：${TimeUnit.MILLISECONDS.toSeconds(firstMs)}s",
            firstMs < 15_000)
        assertTrue("长目录再次打开超过 15 秒：${TimeUnit.MILLISECONDS.toSeconds(repeatedMs)}s",
            repeatedMs < 15_000)
    }
}
