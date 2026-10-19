package com.webnovel.mobile

import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextReplacement
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeRight
import androidx.compose.ui.test.swipeLeft
import com.chaquo.python.Python
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test

/** Emulator-only UI fixture: temporary GET view override, no real favorites file changes. */
class FavoriteFiltersUiTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()
    private lateinit var comic: SelfTestComic
    private var patched = false
    private lateinit var endpoint: EngineEndpoint
    private val namespace by lazy { Python.getInstance().getModule("builtins").callAttr("dict") }
    private fun exec(code: String) {
        Python.getInstance().getModule("builtins").callAttr("exec", code, namespace)
    }
    private fun nodes(text: String) = rule.onAllNodesWithText(text).fetchSemanticsNodes().size

    @Before fun setup() = runBlocking {
        val qemu = ProcessBuilder("getprop", "ro.kernel.qemu").start().inputStream.bufferedReader().readText().trim()
        assertTrue("This fixture must never run on a user's phone", qemu == "1")
        endpoint = (EngineGateway(rule.activity).connect() as EngineState.Ready).endpoint
        MangaFavoriteStartupCheck.claim(endpoint.instanceId)
        comic = SelfTestComic()
        comic.create()
        exec("""
            import app as _app
            from flask import jsonify
            _key = next(k for k in _app.app.view_functions if k.endswith('.api_manga_favorites'))
            _original = _app.app.view_functions[_key]
            _rows = [
                {'source':'mangadex','comic_id':'${comic.comicIdValue}', 'title':'自检漫画 未读', 'cover':'', 'unread_count':6},
                {'source':'mangadex','comic_id':'filter-read', 'title':'已读作品', 'cover':'', 'unread_count':0},
                {'source':'copymanga','comic_id':'filter-other', 'title':'其他作品', 'cover':'', 'unread_count':1}]
            _app.app.view_functions[_key] = lambda: jsonify({'favorites':_rows})
        """.trimIndent())
        patched = true
        rule.activityRule.scenario.recreate()
        Unit
    }

    @After fun cleanup() {
        if (patched) exec("_app.app.view_functions[_key] = _original")
        if (::comic.isInitialized) {
            comic.cleanup()
            assertTrue(!comic.exists())
            assertTrue(comic.libraryEntryCount() == 0)
            assertTrue(comic.historyEntryCount() == 0)
        }
        if (::endpoint.isInitialized) MangaFavoriteStartupCheck.release(endpoint.instanceId)
    }

    @Test fun unreadSearchEmptyClearAndReturnKeepStableSelection() {
        rule.openCachedShelf()
        rule.onNodeWithText("收藏").performClick()
        rule.onNodeWithTag("shelf_tab_2").assertIsSelected()
        rule.waitUntil(30_000) {
            rule.onAllNodesWithTag("favorite_filter_query").fetchSemanticsNodes().isNotEmpty()
        }
        rule.onNodeWithText("全部 3").assertExists()
        if (androidx.test.platform.app.InstrumentationRegistry.getArguments()
                .getString("captureUi") == "true") {
            rule.mainClock.advanceTimeBy(600)
            rule.waitForIdle()
            val automation = androidx.test.platform.app.InstrumentationRegistry
                .getInstrumentation().uiAutomation
            android.os.ParcelFileDescriptor.AutoCloseInputStream(automation.executeShellCommand(
                "screencap -p /sdcard/Download/webnovel-shelf-tabs-qa.png"
            )).use { it.readBytes() }
        }
        rule.onNodeWithTag("favorite_filter_unread").performClick()
        rule.onNodeWithTag("favorite_filter_unread").assertIsSelected()
        assertTrue(nodes("已读作品") == 0)
        rule.onNodeWithTag("favorite_filter_query").performTextReplacement("自检 MangaDex")
        assertTrue(nodes("其他作品") == 0)
        rule.onNodeWithText("自检漫画 未读").performClick()
        rule.waitUntil(30_000) { nodes("查找章节") > 0 }
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("manga_detail_more"))
        rule.onNodeWithTag("manga_detail_more").performClick()
        rule.onNodeWithText("从书库移除").assertExists()
        rule.onNodeWithTag("copy_catalog_report").performClick()
        rule.waitUntil(10_000) { nodes("章节核对信息已复制（含作品名与章节 ID，无图片）；请确认后分享。") > 0 }
        rule.onNodeWithTag("manga_detail_more").performClick()
        rule.onNodeWithText("从书库移除").performClick()
        rule.onNodeWithText("从书库移除？").assertExists()
        rule.onNodeWithTag("remove_keep_files").assertExists()
        rule.onNodeWithText("取消").performClick()
        assertTrue("取消管理操作不能移除本地漫画", comic.libraryEntryCount() == 1)
        rule.runOnUiThread { rule.activity.onBackPressedDispatcher.onBackPressed() }
        rule.waitUntil(30_000) {
            rule.onAllNodesWithTag("favorite_filter_query").fetchSemanticsNodes().isNotEmpty()
        }
        rule.onNodeWithTag("favorite_filter_query").assertTextContains("自检 MangaDex")
        rule.onNodeWithTag("shelf_tab_2").assertIsSelected()
        rule.onNodeWithTag("favorite_filter_unread").assertIsSelected()
        rule.onNodeWithTag("favorite_filter_query").performTextReplacement("不存在")
        rule.onNodeWithText("没有符合当前筛选的收藏").assertExists()
        rule.onNodeWithText("清除筛选").performClick()
        rule.onNodeWithText("自检漫画 未读").assertExists()
        rule.onNodeWithText("已读作品").assertExists()
        rule.onNodeWithText("其他作品").assertExists()
        rule.onNodeWithTag("shelf_pager").performTouchInput { swipeRight() }
        rule.waitUntil(10_000) {
            runCatching { rule.onNodeWithTag("shelf_tab_1").assertIsSelected(); true }.getOrDefault(false)
        }
        rule.onNodeWithTag("shelf_pager").performTouchInput { swipeLeft() }
        rule.waitUntil(10_000) {
            runCatching { rule.onNodeWithTag("shelf_tab_2").assertIsSelected(); true }.getOrDefault(false)
        }
    }
}
