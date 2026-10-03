package com.webnovel.mobile

import android.content.pm.ActivityInfo
import android.content.res.Configuration
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.ExperimentalTestApi
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.performKeyInput
import androidx.compose.ui.test.click
import androidx.compose.ui.test.pressKey
import androidx.compose.ui.input.key.Key
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Verifies reader-only controls start hidden, use the small center target, and idle-hide again. */
@RunWith(AndroidJUnit4::class)
@LargeTest
@OptIn(ExperimentalTestApi::class)
class MangaReaderControlsAutoHideTest {
    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private lateinit var comic: SelfTestComic

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size

    private fun waitFor(timeoutMs: Long = 30_000, condition: () -> Boolean) {
        rule.waitUntil(timeoutMs, condition)
    }

    private fun rotateTo(requested: Int, expected: Int) {
        rule.activityRule.scenario.onActivity { it.requestedOrientation = requested }
        waitFor(timeoutMs = 20_000) {
            var orientation = Configuration.ORIENTATION_UNDEFINED
            rule.activityRule.scenario.onActivity {
                orientation = it.resources.configuration.orientation
            }
            orientation == expected
        }
    }

    @Before
    fun setUp() {
        comic = SelfTestComic()
        comic.create()
        rule.activityRule.scenario.recreate()
    }

    @After
    fun tearDown() {
        comic.cleanup()
        assertFalse("自检漫画目录未清理干净", comic.exists())
        assertTrue("自检书库条目未清理", comic.libraryEntryCount() == 0)
        assertTrue("自检历史条目未清理", comic.historyEntryCount() == 0)
    }

    @Test
    fun readerControlsStartHiddenRevealAtCenterAndHideAfterIdle() {
        ReaderPrefs.save(rule.activity, ReaderPrefs.load(rule.activity).copy(horizontalPaging = true))
        rule.openCachedShelf()
        val shelfTag = "manga_cached:${comic.sourceKey}:${comic.comicIdValue}"
        waitFor { nodes(shelfTag) > 0 }
        rule.onNodeWithTag(shelfTag).performClick()
        waitFor { rule.onAllNodesWithText("开始阅读").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithText("开始阅读").performClick()

        val pageTag = if (runCatching { nodes("manga_pages") > 0 }.getOrDefault(false)) {
            "manga_pages"
        } else "manga_pager"
        waitFor { nodes(pageTag) > 0 }
        assertEquals(0, nodes("reader_progress_label"))

        // Ordinary page-area taps outside the deliberately small center hotspot must not reveal bars.
        rule.onNodeWithTag(pageTag).performTouchInput {
            click(androidx.compose.ui.geometry.Offset(width * 0.25f, height * 0.5f))
        }
        assertEquals(0, nodes("reader_progress_label"))

        rule.onNodeWithTag(pageTag).performTouchInput {
            click(androidx.compose.ui.geometry.Offset(width / 2f, height / 2f))
        }
        waitFor(timeoutMs = 10_000) { nodes("reader_progress_label") > 0 }
        waitFor(timeoutMs = 10_000) { nodes("reader_progress_label") == 0 }

        // The timeout is reader-scoped; reopening remains possible with the same small center tap.
        rule.onNodeWithTag(pageTag).performTouchInput { click() }
        waitFor(timeoutMs = 10_000) { nodes("reader_progress_label") > 0 }

        assertTrue("两页本地章节应显示可调节进度滑杆", nodes("reader_progress_slider") > 0)
        rule.onNodeWithTag("reader_progress_slider").performTouchInput {
            click(androidx.compose.ui.geometry.Offset(width * 0.96f, height / 2f))
        }
        waitFor(timeoutMs = 10_000) {
            rule.onAllNodesWithText("2/2页", substring = true).fetchSemanticsNodes().isNotEmpty()
        }

        // Hardware direction keys use the same page ordering as touch in paged mode.
        rule.onNodeWithTag(pageTag).performKeyInput { pressKey(Key.DirectionLeft) }
        waitFor(timeoutMs = 10_000) {
            rule.onAllNodesWithText("1/2页", substring = true).fetchSemanticsNodes().isNotEmpty()
        }
        rule.onNodeWithTag(pageTag).performKeyInput { pressKey(Key.DirectionRight) }
        waitFor(timeoutMs = 10_000) {
            rule.onAllNodesWithText("2/2页", substring = true).fetchSemanticsNodes().isNotEmpty()
        }

        // Force real configuration changes in both directions. This recreates MainActivity
        // on the API35 AVD and must preserve the reader route and current page.
        rotateTo(ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE,
            Configuration.ORIENTATION_LANDSCAPE)
        waitFor(timeoutMs = 30_000) {
            nodes("manga_pages") > 0 || nodes("manga_pager") > 0
        }
        assertEquals("重建后阅读控件仍应默认隐藏", 0, nodes("reader_progress_label"))
        rule.tapReaderCenterAndShowControls()
        waitFor(timeoutMs = 15_000) {
            rule.onAllNodesWithText("2/2页", substring = true).fetchSemanticsNodes().isNotEmpty()
        }
        rotateTo(ActivityInfo.SCREEN_ORIENTATION_PORTRAIT,
            Configuration.ORIENTATION_PORTRAIT)
        waitFor(timeoutMs = 30_000) {
            nodes("manga_pages") > 0 || nodes("manga_pager") > 0
        }
        assertEquals("返回竖屏后阅读控件仍应默认隐藏", 0, nodes("reader_progress_label"))
        rule.tapReaderCenterAndShowControls()
        waitFor(timeoutMs = 15_000) {
            rule.onAllNodesWithText("2/2页", substring = true).fetchSemanticsNodes().isNotEmpty()
        }
    }
}
