package com.webnovel.mobile

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.chaquo.python.Python
import java.io.File
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

/** Real Compose + embedded HTTP: missing local decoration must not invoke source fetches. */
class MangaLocalCoverUiTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()
    private var comic: SelfTestComic? = null
    private var guardInstalled = false
    private val namespace by lazy { Python.getInstance().getModule("builtins").callAttr("dict") }
    private fun exec(code: String) {
        Python.getInstance().getModule("builtins").callAttr("exec", code, namespace)
    }

    @After fun cleanup() {
        try {
            if (guardInstalled) exec("""
                _app.app.before_request_funcs[None].remove(_guard)
                _api._library_cover_url = _original_cover_url
            """.trimIndent())
        } finally {
            comic?.let {
                it.cleanup()
                assertFalse("隔离漫画仍留在磁盘", it.exists())
                assertTrue("隔离漫画仍留在书库", it.libraryEntryCount() == 0)
                assertTrue("隔离漫画仍留在历史", it.historyEntryCount() == 0)
            }
        }
    }

    @Test fun missingLocalCoverShowsPlaceholderWithoutOnlineFallbackAndCanRead() {
        val gateway = EngineGateway(rule.activity.applicationContext)
        val endpoint = (runBlocking { gateway.connect() } as EngineState.Ready).endpoint
        val c = SelfTestComic().also { comic = it; it.create() }
        val info = File(rule.activity.filesDir,
            "runtime/manga/downloads/${c.sourceKey}/${c.comicIdValue}/_info.json")
        val remote = "https://selftest.invalid/${c.comicIdValue}/cover.jpg"
        info.writeText(JSONObject(info.readText()).put("cover", remote).toString())
        // Record real engine requests; fail closed only for this fixture's forbidden
        // remote path so a regression cannot accidentally contact an outside server.
        exec("""
            import app as _app
            import server.manga_api as _api
            from flask import request, jsonify
            _events = []
            _original_cover_url = _api._library_cover_url
            def _cover_url(source, cid):
                if cid == '${c.comicIdValue}':
                    _events.append('REMOTE_METADATA')
                    return ''
                return _original_cover_url(source, cid)
            def _guard():
                if request.path == '/api/manga/${c.sourceKey}/${c.comicIdValue}/cover':
                    _events.append('COVER:' + request.args.get('catalog', ''))
                if request.path == '/api/manga/cover' and request.args.get('url') == '$remote':
                    _events.append('REMOTE_PROXY')
                    return jsonify(error='forbidden selftest outbound request'), 502
            _api._library_cover_url = _cover_url
            _app.app.before_request_funcs.setdefault(None, []).append(_guard)
        """.trimIndent())
        guardInstalled = true
        val detail = runBlocking { gateway.httpText(endpoint.port, c.detailUrl) }
        assertTrue(detail.ok)
        assertTrue(JSONObject(detail.body).getString("cover") == remote)
        assertTrue(JSONObject(detail.body).getString("cover_view").isEmpty())

        rule.openCachedShelf()
        val tag = "manga_cached:${c.sourceKey}:${c.comicIdValue}"
        rule.waitUntil(60_000) { rule.onAllNodesWithTag(tag).fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag(tag).performClick()
        // This is the final Coil error state, not an early check while the image is loading.
        rule.waitUntil(30_000) { rule.onAllNodesWithText("暂无封面").fetchSemanticsNodes().isNotEmpty() }
        exec("assert _events == ['COVER:local'], _events")
        rule.onNodeWithText("开始阅读").performClick()
        rule.waitUntil(30_000) {
            rule.onAllNodesWithTag("manga_pages").fetchSemanticsNodes().isNotEmpty() ||
                rule.onAllNodesWithTag("manga_pager").fetchSemanticsNodes().isNotEmpty()
        }
        exec("assert _events == ['COVER:local'], _events")
        println("MANGA_LOCAL_COVER primary_catalog_local=true remote_metadata=0 remote_proxy=0 reader_open=true")
    }
}
