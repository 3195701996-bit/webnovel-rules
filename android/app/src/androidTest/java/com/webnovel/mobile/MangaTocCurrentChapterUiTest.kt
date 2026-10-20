package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.click
import androidx.compose.ui.test.getUnclippedBoundsInRoot
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.hasText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** A long TOC should open at the resumed chapter, not force the reader back to chapter one. */
@RunWith(AndroidJUnit4::class)
@LargeTest
class MangaTocCurrentChapterUiTest {
    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private lateinit var comic: SelfTestComic

    private fun waitFor(timeoutMs: Long = 30_000, condition: () -> Boolean) {
        rule.waitUntil(timeoutMs, condition)
    }

    @Before
    fun setUp() {
        val qemu = ProcessBuilder("getprop", "ro.kernel.qemu").start()
            .inputStream.bufferedReader().use { it.readText().trim() }
        assertTrue("长目录合成阅读测试只能在专用模拟器运行", qemu == "1")
        comic = SelfTestComic(chapterCount = 30, resumeAtChapter = 30)
        comic.create()
        rule.activityRule.scenario.recreate()
    }

    @After
    fun tearDown() {
        if (!::comic.isInitialized) return
        comic.cleanup()
        assertFalse("长目录自检漫画未清理", comic.exists())
        assertTrue("长目录自检书库条目未清理", comic.libraryEntryCount() == 0)
        assertTrue("长目录自检历史条目未清理", comic.historyEntryCount() == 0)
    }

    @Test
    fun opensLongTocAtResumedChapter() {
        rule.openCachedShelf()
        val shelfTag = "manga_cached:${comic.sourceKey}:${comic.comicIdValue}"
        waitFor { rule.onAllNodesWithTag(shelfTag).fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag(shelfTag).performClick()
        waitFor { rule.onAllNodesWithText("继续阅读 第 30 话").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithText("继续阅读 第 30 话").performClick()
        waitFor(timeoutMs = 60_000) {
            rule.onAllNodesWithTag("manga_pages").fetchSemanticsNodes().isNotEmpty() ||
                rule.onAllNodesWithTag("manga_pager").fetchSemanticsNodes().isNotEmpty()
        }

        // ReaderPrefs persist across instrumentation methods. Either presentation is
        // valid here; the test concerns chapter identity and TOC positioning, not mode.
        val readerTag = if (rule.onAllNodesWithTag("manga_pager")
                .fetchSemanticsNodes().isNotEmpty()) "manga_pager" else "manga_pages"
        rule.onNodeWithTag(readerTag).performTouchInput {
            click(androidx.compose.ui.geometry.Offset(width / 2f, height / 2f))
        }
        waitFor { rule.onAllNodesWithTag("reader_progress_label").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("reader_progress_label").assertTextContains("30 / 30", substring = true)
        waitFor { rule.onAllNodesWithText("目录").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithText("目录").performClick()
        waitFor { rule.onAllNodesWithTag("manga_toc").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithText("目录（30 个单元）").assertIsDisplayed()
        rule.onNodeWithText("第 30 话").assertIsDisplayed()
        rule.onNodeWithText(SelfTestComic.CH1_NAME).assertDoesNotExist()

        // Prove the resumed chapter starts near the top of the actual TOC viewport,
        // rather than merely being visible at its lower edge.
        val viewport = rule.onNodeWithTag("manga_toc").getUnclippedBoundsInRoot()
        rule.onNodeWithTag("manga_toc_current_chapter").assertIsDisplayed()
        val resumedChapter = rule.onNodeWithTag("manga_toc_current_chapter").getUnclippedBoundsInRoot()
        val viewportHeight = viewport.bottom - viewport.top
        assertTrue(
            "续读章节应定位在目录视口上方三分之一内：viewport=$viewport, item=$resumedChapter",
            resumedChapter.top < viewport.top + viewportHeight * 0.35f,
        )
        // An explicit earlier selection must replace chapter 30, not be treated
        // as another request to resume the old record.
        rule.onNodeWithTag("manga_toc").performScrollToNode(hasText(SelfTestComic.CH1_NAME, substring = true))
        rule.onNodeWithText(SelfTestComic.CH1_NAME).performClick()
        waitFor { rule.onAllNodesWithTag("manga_toc").fetchSemanticsNodes().isEmpty() }
        rule.tapReaderCenterAndShowControls()
        rule.onNodeWithTag("reader_progress_label").assertTextContains("1 / 30", substring = true)
        rule.onNodeWithTag("reader_progress_label").assertTextContains("1/2页", substring = true)
    }

    @Test
    fun detailChapterSelectionOverridesLaterResumeRecord() {
        rule.openCachedShelf()
        val shelfTag = "manga_cached:${comic.sourceKey}:${comic.comicIdValue}"
        waitFor { rule.onAllNodesWithTag(shelfTag).fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag(shelfTag).performClick()
        waitFor { rule.onAllNodesWithText("继续阅读 第 30 话").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasText(SelfTestComic.CH1_NAME, substring = true))
        rule.onNodeWithText(SelfTestComic.CH1_NAME).performClick()
        waitFor(timeoutMs = 60_000) {
            rule.onAllNodesWithTag("manga_pages").fetchSemanticsNodes().isNotEmpty() ||
                rule.onAllNodesWithTag("manga_pager").fetchSemanticsNodes().isNotEmpty()
        }
        rule.tapReaderCenterAndShowControls()
        rule.onNodeWithTag("reader_progress_label").assertTextContains("1 / 30", substring = true)
        rule.onNodeWithTag("reader_progress_label").assertTextContains("1/2页", substring = true)
    }
}
