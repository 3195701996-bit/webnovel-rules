package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import com.chaquo.python.Python
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Rule
import org.junit.Test

class PendingChapterUiTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()
    private val key = "mangadex:qa_pending_${System.nanoTime()}"
    private var installed = false
    private val namespace by lazy { Python.getInstance().getModule("builtins").callAttr("dict") }
    private fun exec(code: String) {
        Python.getInstance().getModule("builtins").callAttr("exec", code, namespace)
    }

    @After fun cleanup() {
        if (installed) exec("""
            with _m._lock:
                _m._tasks.pop('$key', None)
            _m.save()
        """.trimIndent())
    }

    @Test fun pendingCountAppearsInNativeDownloadQueue() {
        val gateway = EngineGateway(rule.activity.applicationContext)
        runBlocking { gateway.connect() }
        exec("""
            import server.state as _s
            _m = _s._manga_dl
            with _m._lock:
                assert '$key' not in _m._tasks
                _m._tasks['$key'] = {'status':'running', 'source':'mangadex',
                    'comic_id':'${key.substringAfter(':')}', 'title':'追加队列验收',
                    'done':1, 'total':3, 'chapters':['ch41'],
                    'pending_chapters':['ch42','ch43']}
            _m.save()
        """.trimIndent())
        installed = true
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("nav_download").fetchSemanticsNodes().isNotEmpty() }
        rule.onAllNodesWithTag("nav_download")[0].performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("downloads_list").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("downloads_list").performScrollToNode(hasText("待补 2 章 · 当前批完成后继续"))
        rule.onNodeWithText("待补 2 章 · 当前批完成后继续").assertIsDisplayed()
    }
}
