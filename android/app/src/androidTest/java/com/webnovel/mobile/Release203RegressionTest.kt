package com.webnovel.mobile

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.click
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performScrollToIndex
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.assertIsOff
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.getUnclippedBoundsInRoot
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Before
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class Release203RegressionTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()
    private var comic: SelfTestComic? = null
    private var book: SelfTestBook? = null
    private lateinit var gateway: EngineGateway
    private lateinit var endpoint: EngineEndpoint
    private var updateFixtureInstalled = false
    private var deleteFixtureInstalled = false
    private var localMediaFixtureInstalled = false
    private var updateFixtureNamespace: com.chaquo.python.PyObject? = null

    @Before fun requireIsolatedEmulator() {
        val device = InstrumentationRegistry.getInstrumentation().uiAutomation
        val descriptor = device.executeShellCommand("getprop ro.kernel.qemu")
        val qemu = android.os.ParcelFileDescriptor.AutoCloseInputStream(descriptor)
            .bufferedReader().use { it.readText().trim() }
        assertTrue("合成数据回归只能运行于专用模拟器，不得运行于用户真机", qemu == "1")
    }

    private fun execUpdateFixture(code: String) {
        val builtins = com.chaquo.python.Python.getInstance().getModule("builtins")
        if (updateFixtureNamespace == null) updateFixtureNamespace = builtins.callAttr("dict")
        builtins.callAttr("exec", code, updateFixtureNamespace)
    }

    private fun connect() {
        gateway = EngineGateway(rule.activity.applicationContext)
        endpoint = (runBlocking { gateway.connect() } as EngineState.Ready).endpoint
    }

    @After fun cleanup() {
        if (localMediaFixtureInstalled) {
            execUpdateFixture("_qa_flask.view_functions[_qa_media_key] = _qa_media_original")
        }
        if (deleteFixtureInstalled) {
            execUpdateFixture("_qa_delete_release.set()")
            execUpdateFixture("_qa_flask.view_functions[_qa_delete_key] = _qa_delete_original")
        }
        if (updateFixtureInstalled) {
            execUpdateFixture("""
                import server.manga_api as _qa_api
                if hasattr(_qa_api, '_qa_original_update_check'):
                    _qa_api._manga_check_one = _qa_api._qa_original_update_check
                    del _qa_api._qa_original_update_check
            """.trimIndent())
        }
        comic?.let { c ->
            runBlocking { gateway.httpDelete(endpoint.port, "/api/manga/favorites/${c.sourceKey}/${c.comicIdValue}") }
            c.cleanup()
        }
        book?.cleanup()
    }

    @Test fun cancelFavoriteFromDownloadedDetailPersists() {
        connect()
        val c = SelfTestComic().also { comic = it; it.create() }
        val added = runBlocking { gateway.httpPost(endpoint.port, "/api/manga/favorites",
            JSONObject().put("source", c.sourceKey).put("comic_id", c.comicIdValue)
                .put("title", SelfTestComic.TITLE).toString()) }
        assertTrue(added.ok)
        rule.openCachedShelf()
        val tag = "manga_cached:${c.sourceKey}:${c.comicIdValue}"
        rule.waitUntil(60_000) { rule.onAllNodesWithTag(tag).fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag(tag).performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithText("取消收藏").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithText("取消收藏").performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithText("已取消收藏").fetchSemanticsNodes().isNotEmpty() }
        val response = runBlocking { gateway.httpText(endpoint.port, "/api/manga/favorites") }
        assertTrue(response.ok)
        assertTrue(EngineData.mangaFavorites(response.body).none { it.comicId == c.comicIdValue })
        assertTrue("取消收藏不得删除本地漫画", c.exists())
        println("RELEASE203 favorite_cancel_persisted_local_data_preserved")
    }

    @Test fun localDeleteSelectAllCanBeReversedWithoutDeletingFiles() {
        connect()
        val c = SelfTestComic().also { comic = it; it.create() }
        rule.openCachedShelf()
        val tag = "manga_cached:${c.sourceKey}:${c.comicIdValue}"
        rule.waitUntil(60_000) { rule.onAllNodesWithTag(tag).fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag(tag).performClick()
        rule.waitUntil(30_000) {
            rule.onAllNodesWithTag("manga_detail_list").fetchSemanticsNodes().isNotEmpty()
        }
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("manage_downloaded"))
        val readBounds = rule.onNodeWithTag("manga_read_primary").getUnclippedBoundsInRoot()
        val manageBounds = rule.onNodeWithTag("manage_downloaded").getUnclippedBoundsInRoot()
        assertTrue("阅读和下载管理应并排", kotlin.math.abs(readBounds.top.value - manageBounds.top.value) < 1f)
        assertTrue("主要操作必须保留触控高度",
            (readBounds.bottom - readBounds.top).value >= 48f &&
                (manageBounds.bottom - manageBounds.top).value >= 48f)
        val checkBounds = rule.onNodeWithTag("check_update").getUnclippedBoundsInRoot()
        val moreBounds = rule.onNodeWithTag("manga_detail_more").getUnclippedBoundsInRoot()
        assertTrue("标准手机宽度次要操作应在同一行", kotlin.math.abs(checkBounds.top.value - moreBounds.top.value) < 1f)
        if (InstrumentationRegistry.getArguments().getString("captureDetailUi") == "true") {
            val output = InstrumentationRegistry.getInstrumentation().uiAutomation
                .executeShellCommand("screencap -p /sdcard/Download/webnovel-detail-actions-qa.png")
            android.os.ParcelFileDescriptor.AutoCloseInputStream(output).use { it.readBytes() }
        }
        rule.onNodeWithTag("manage_downloaded").performClick()
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("select_all_downloaded"))
        rule.onNodeWithTag("select_all_downloaded").performClick()
        rule.onNodeWithText("取消全选").assertIsDisplayed()
        rule.onNodeWithText("删除选中（2）").assertIsDisplayed()
        rule.onNodeWithTag("delete_selected").performClick()
        rule.onNodeWithText("删除选中的已下载内容？").assertIsDisplayed()
        rule.onNodeWithTag("delete_confirm").assertIsDisplayed()
        assertTrue("确认之前不能删除本地内容", c.exists())
        rule.onNodeWithText("取消", substring = false).performClick()
        assertTrue("取消确认必须保留本地内容", c.exists())
        rule.onNodeWithText("删除选中（2）").assertIsDisplayed()
        rule.onNodeWithTag("select_all_downloaded").performClick()
        rule.onNodeWithText("全选本地内容").assertIsDisplayed()
        rule.onNodeWithText("删除选中（0）").assertIsDisplayed()
        assertTrue("选择与取消选择不能删除本地文件", c.exists())
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("manga_detail_more"))
        rule.onNodeWithTag("manga_detail_more").performClick()
        rule.onNodeWithTag("copy_catalog_report").performClick()
        rule.waitUntil(10_000) {
            rule.onAllNodesWithText("章节核对信息已复制", substring = true).fetchSemanticsNodes().isNotEmpty()
        }
        rule.runOnIdle {
            val clipboard = rule.activity.getSystemService(android.content.Context.CLIPBOARD_SERVICE)
                as android.content.ClipboardManager
            val report = JSONObject(clipboard.primaryClip!!.getItemAt(0).text.toString())
            assertTrue(report.getString("comic_id") == c.comicIdValue)
            assertTrue(report.getJSONArray("chapters").length() == 2)
            assertTrue(!report.has("cover") && !report.has("images"))
        }
    }

    @Test fun partialVolumeCanBeSelectedAndDeselectedWithoutChangingMedia() {
        connect()
        val c = SelfTestComic(resumeAtChapter = 2).also { comic = it; it.create(); it.makeFirstUnitPartialVolume() }
        val before = c.mediaDigest()
        // Local reading catalogs deliberately exclude incomplete media. Manage
        // the partial unit from the full catalog/history entry, not by weakening
        // that strict local-reading contract.
        val response = runBlocking { gateway.httpText(endpoint.port, c.detailUrl.substringBefore('?')) }
        assertTrue(response.ok)
        val detail = EngineData.mangaDetail(response.body)!!
        assertTrue("夹具必须真实被服务端判为部分整卷：${response.body}",
            detail.volumes.any { it.id == SelfTestComic.CH1_ID } &&
                detail.partialDownloaded.contains(SelfTestComic.CH1_ID) &&
                !detail.downloaded.contains(SelfTestComic.CH1_ID))
        rule.openCachedShelf()
        rule.onAllNodesWithText("最近阅读")[0].performClick()
        rule.waitUntil(60_000) { rule.onAllNodesWithText(SelfTestComic.TITLE).fetchSemanticsNodes().isNotEmpty() }
        rule.onAllNodesWithText(SelfTestComic.TITLE)[0].performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("manage_downloaded").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("manage_downloaded").performClick()
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("pick_${SelfTestComic.CH1_ID}"))
        rule.onNodeWithTag("volume_status_${SelfTestComic.CH1_ID}", useUnmergedTree = true)
            .assertTextEquals("部分下载 · 可补齐")
        rule.onNodeWithTag("pick_${SelfTestComic.CH1_ID}").assertIsOff().performClick().assertIsOn()
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("select_all_downloaded"))
        rule.onNodeWithText("删除选中（1）").assertIsDisplayed()
        rule.onNodeWithTag("select_all_downloaded").performClick()
        rule.onNodeWithText("删除选中（2）").assertIsDisplayed()
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("pick_${SelfTestComic.CH1_ID}"))
        rule.onNodeWithTag("pick_${SelfTestComic.CH1_ID}").assertIsOn().performClick().assertIsOff()
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("select_all_downloaded"))
        rule.onNodeWithText("删除选中（1）").assertIsDisplayed()
        assertTrue("选择与取消不得改变本地图片字节", before == c.mediaDigest())
    }

    @Test fun localMediaManagerCanDeletePartialVolumeWithoutChangingReadingHistory() {
        connect()
        val c = SelfTestComic(resumeAtChapter = 2).also { comic = it; it.create(); it.makeFirstUnitPartialVolume() }
        val added = runBlocking { gateway.httpPost(endpoint.port, "/api/manga/favorites",
            JSONObject().put("source", c.sourceKey).put("comic_id", c.comicIdValue)
                .put("title", SelfTestComic.TITLE).toString()) }
        assertTrue(added.ok)
        val keptSize = c.imageSize(SelfTestComic.CH2_ID, 0)
        val keptDigest = c.imageDigest(SelfTestComic.CH2_ID, 0)
        val before = c.mediaDigest()
        val local = runBlocking { gateway.httpText(endpoint.port, c.detailUrl) }
        assertTrue(EngineData.mangaDetail(local.body)!!.readingChapters.none { it.id == SelfTestComic.CH1_ID })
        rule.openCachedShelf()
        val tag = "manga_cached:${c.sourceKey}:${c.comicIdValue}"
        rule.waitUntil(60_000) { rule.onAllNodesWithTag(tag).fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag(tag).performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("manga_detail_more").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("manga_detail_more"))
        rule.onNodeWithTag("manga_detail_more").performClick()
        rule.onNodeWithTag("manage_local_media").performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("local_media_pick_${SelfTestComic.CH1_ID}").fetchSemanticsNodes().isNotEmpty() }
        if (InstrumentationRegistry.getArguments().getString("captureLocalMediaUi") == "true") {
            rule.mainClock.advanceTimeBy(600)
            rule.waitForIdle()
            android.os.SystemClock.sleep(250)
            val output = InstrumentationRegistry.getInstrumentation().uiAutomation
                .executeShellCommand("screencap -p /sdcard/Download/webnovel-local-media-qa.png")
            android.os.ParcelFileDescriptor.AutoCloseInputStream(output).use { it.readBytes() }
        }
        rule.onNodeWithTag("local_media_select_all").performClick()
        rule.onNodeWithText("删除选中（2）").assertIsDisplayed()
        rule.onNodeWithTag("local_media_select_all").performClick()
        rule.onNodeWithTag("local_media_pick_${SelfTestComic.CH1_ID}").performClick()
        rule.onNodeWithTag("local_media_delete").performClick()
        rule.onNodeWithText("删除选中的本地缓存？").assertIsDisplayed()
        rule.onNodeWithText("取消", substring = false).performClick()
        assertTrue(before == c.mediaDigest())
        rule.onNodeWithTag("local_media_delete").performClick()
        rule.onNodeWithTag("local_media_delete_confirm").performClick()
        rule.waitUntil(30_000) {
            rule.onAllNodesWithText("已删除 1 项本地内容").fetchSemanticsNodes().isNotEmpty() &&
                rule.onAllNodesWithTag("local_media_pick_${SelfTestComic.CH1_ID}").fetchSemanticsNodes().isEmpty()
        }
        assertTrue(c.imageSize(SelfTestComic.CH1_ID, 0) == 0L)
        assertTrue(c.imageSize(SelfTestComic.CH1_ID, 1) == 0L)
        assertTrue(c.imageSize(SelfTestComic.CH2_ID, 0) == keptSize)
        assertTrue(c.imageDigest(SelfTestComic.CH2_ID, 0) == keptDigest)
        val favorites = runBlocking { gateway.httpText(endpoint.port, "/api/manga/favorites") }
        assertTrue(EngineData.mangaFavorites(favorites.body).any { it.comicId == c.comicIdValue })
        val history = runBlocking { gateway.httpText(endpoint.port, "/api/manga/history") }
        assertTrue(EngineData.mangaHistory(history.body).any {
            it.comicId == c.comicIdValue && SelfTestComic.CH2_ID in it.readChapterIds && it.idx == 1 })
        rule.onNodeWithTag("local_media_close").performClick()
    }

    @Test fun longLocalMediaInventoryRemainsScrollableAndSelectsExactRows() {
        if (InstrumentationRegistry.getArguments().getString("localMediaLandscape") == "true") {
            rule.runOnUiThread {
                rule.activity.requestedOrientation = android.content.pm.ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE
            }
            rule.waitUntil(30_000) {
                rule.activity.resources.configuration.orientation == android.content.res.Configuration.ORIENTATION_LANDSCAPE
            }
        }
        connect()
        val c = SelfTestComic().also { comic = it; it.create() }
        val before = c.mediaDigest()
        execUpdateFixture("""
            import app as _qa_app_module
            from flask import jsonify as _qa_jsonify
            _qa_flask = _qa_app_module.app
            _qa_media_key = next(k for k in _qa_flask.view_functions if k.endswith('.api_manga_local_media'))
            _qa_media_original = _qa_flask.view_functions[_qa_media_key]
            def _qa_long_media(source, comic_id):
                if comic_id == '${c.comicIdValue}':
                    return _qa_jsonify({'ok': True, 'local_only': True, 'source': source,
                        'comic_id': comic_id, 'count': 1000, 'media': [
                            {'id': 'qa-long-' + str(i), 'name': '第' + str(i + 1) + '话 · 长标题缓存管理滚动验收',
                             'downloaded': i % 2 == 0, 'partial': i % 2 != 0} for i in range(1000)]})
                return _qa_media_original(source, comic_id)
            _qa_flask.view_functions[_qa_media_key] = _qa_long_media
        """.trimIndent())
        localMediaFixtureInstalled = true
        rule.openCachedShelf()
        val tag = "manga_cached:${c.sourceKey}:${c.comicIdValue}"
        rule.waitUntil(60_000) { rule.onAllNodesWithTag(tag).fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag(tag).performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("manga_detail_more").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("manga_detail_more"))
        rule.onNodeWithTag("manga_detail_more").performClick()
        rule.onNodeWithTag("manage_local_media").performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("local_media_pick_qa-long-0").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("local_media_select_all").performClick()
        rule.onNodeWithTag("local_media_delete").assertTextEquals("删除选中（1000）").assertIsDisplayed()
        rule.onNodeWithTag("local_media_list").performScrollToIndex(999)
        rule.onNodeWithTag("local_media_pick_qa-long-999").assertIsDisplayed().assertIsOn().performClick()
        rule.onNodeWithTag("local_media_delete").assertTextEquals("删除选中（999）").assertIsDisplayed()
        rule.onNodeWithTag("local_media_close").assertIsDisplayed().performClick()
        assertTrue(before == c.mediaDigest())
    }

    @Test fun localMediaReadAndDeleteFailuresRecoverWithoutLosingSelection() {
        connect()
        val c = SelfTestComic().also { comic = it; it.create(); it.makeFirstUnitPartialVolume() }
        val before = c.mediaDigest()
        execUpdateFixture("""
            import app as _qa_app_module
            import threading as _qa_threading
            from flask import jsonify as _qa_jsonify
            _qa_flask = _qa_app_module.app
            _qa_media_key = next(k for k in _qa_flask.view_functions if k.endswith('.api_manga_local_media'))
            _qa_media_original = _qa_flask.view_functions[_qa_media_key]
            _qa_delete_key = next(k for k in _qa_flask.view_functions if k.endswith('.api_manga_chapters_delete'))
            _qa_delete_original = _qa_flask.view_functions[_qa_delete_key]
            _qa_delete_release = _qa_threading.Event()
            _qa_media_attempts = 0
            _qa_delete_attempts = 0
            def _qa_media_once(source, comic_id):
                global _qa_media_attempts
                if comic_id == '${c.comicIdValue}':
                    _qa_media_attempts += 1
                    if _qa_media_attempts == 1:
                        return _qa_jsonify({'error': 'fixture inventory unavailable'}), 503
                return _qa_media_original(source, comic_id)
            def _qa_delete_once(source, comic_id):
                global _qa_delete_attempts
                if comic_id == '${c.comicIdValue}':
                    _qa_delete_attempts += 1
                    if _qa_delete_attempts == 1:
                        _qa_delete_release.wait(15)
                        return _qa_jsonify({'ok': False, 'error': 'fixture locked file'}), 500
                return _qa_delete_original(source, comic_id)
            _qa_flask.view_functions[_qa_media_key] = _qa_media_once
            _qa_flask.view_functions[_qa_delete_key] = _qa_delete_once
        """.trimIndent())
        localMediaFixtureInstalled = true
        deleteFixtureInstalled = true
        rule.openCachedShelf()
        val tag = "manga_cached:${c.sourceKey}:${c.comicIdValue}"
        rule.waitUntil(60_000) { rule.onAllNodesWithTag(tag).fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag(tag).performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("manga_detail_more").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("manga_detail_more"))
        rule.onNodeWithTag("manga_detail_more").performClick()
        rule.onNodeWithTag("manage_local_media").performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("local_media_retry").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("local_media_delete").assertIsNotEnabled()
        assertTrue(rule.onAllNodesWithTag("local_media_pick_${SelfTestComic.CH1_ID}").fetchSemanticsNodes().isEmpty())
        rule.onNodeWithTag("local_media_retry").performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("local_media_pick_${SelfTestComic.CH1_ID}").fetchSemanticsNodes().isNotEmpty() }
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("local_media_status").fetchSemanticsNodes().isEmpty() }
        rule.onNodeWithTag("local_media_pick_${SelfTestComic.CH1_ID}").performClick()
        rule.onNodeWithTag("local_media_delete").performClick()
        rule.onNodeWithTag("local_media_delete_confirm").performClick()
        try {
            rule.onNodeWithTag("local_media_select_all").assertIsNotEnabled()
            rule.onNodeWithTag("local_media_close").assertIsNotEnabled()
            rule.onNodeWithTag("local_media_pick_${SelfTestComic.CH1_ID}").assertIsNotEnabled()
        } finally {
            execUpdateFixture("_qa_delete_release.set()")
        }
        rule.waitUntil(30_000) { rule.onAllNodesWithText("fixture locked file", substring = true).fetchSemanticsNodes().isNotEmpty() }
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("local_media_pick_${SelfTestComic.CH1_ID}").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("local_media_pick_${SelfTestComic.CH1_ID}").assertIsOn()
        rule.onNodeWithTag("local_media_close").assertIsEnabled()
        assertTrue(before == c.mediaDigest())
        rule.onNodeWithTag("local_media_delete").performClick()
        rule.onNodeWithTag("local_media_delete_confirm").performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithText("已删除 1 项本地内容").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("local_media_close").performClick()
    }

    @Test fun failedDeleteKeepsSelectionAndAllowsRetry() {
        connect()
        val c = SelfTestComic().also { comic = it; it.create() }
        execUpdateFixture("""
            import app as _qa_app_module
            import threading as _qa_threading
            from flask import jsonify as _qa_jsonify
            _qa_delete_release = _qa_threading.Event()
            _qa_flask = _qa_app_module.app
            _qa_delete_key = next(k for k in _qa_flask.view_functions if k.endswith('.api_manga_chapters_delete'))
            _qa_delete_original = _qa_flask.view_functions[_qa_delete_key]
            def _qa_failed_delete(source, comic_id):
                if comic_id == '${c.comicIdValue}':
                    _qa_delete_release.wait(15)
                    return _qa_jsonify({'ok': False, 'error': 'fixture locked file'}), 500
                return _qa_delete_original(source, comic_id)
            _qa_flask.view_functions[_qa_delete_key] = _qa_failed_delete
        """.trimIndent())
        deleteFixtureInstalled = true
        rule.openCachedShelf()
        val tag = "manga_cached:${c.sourceKey}:${c.comicIdValue}"
        rule.waitUntil(60_000) { rule.onAllNodesWithTag(tag).fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag(tag).performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("manga_detail_list").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("manage_downloaded"))
        rule.onNodeWithTag("manage_downloaded").performClick()
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("select_all_downloaded"))
        rule.onNodeWithTag("select_all_downloaded").performClick()
        rule.onNodeWithTag("delete_selected").performClick()
        rule.onNodeWithTag("delete_confirm").performClick()
        try {
            rule.onNodeWithTag("manage_downloaded").assertIsNotEnabled()
            rule.onNodeWithTag("select_all_downloaded").assertIsNotEnabled()
            rule.onNodeWithTag("delete_selected").assertIsNotEnabled()
            rule.onNodeWithTag("manga_read_primary").assertIsNotEnabled()
            rule.onNodeWithTag("check_update").assertIsNotEnabled()
            rule.onNodeWithTag("manga_detail_more").assertIsNotEnabled()
        } finally {
            execUpdateFixture("_qa_delete_release.set()")
        }
        rule.waitUntil(30_000) {
            rule.onAllNodesWithText("删除未完成", substring = true).fetchSemanticsNodes().isNotEmpty()
        }
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("delete_selected"))
        rule.onNodeWithText("删除选中（2）").assertIsDisplayed()
        rule.onNodeWithTag("manage_downloaded").assertIsEnabled()
        rule.onNodeWithTag("delete_selected").assertIsEnabled()
        rule.onNodeWithTag("manga_detail_more").assertIsEnabled()
        assertTrue("失败不能删除夹具文件", c.exists())
        rule.onNodeWithTag("delete_selected").performClick()
        rule.onNodeWithTag("delete_confirm").assertIsDisplayed()
        rule.onNodeWithText("取消", substring = false).performClick()
    }

    @Test fun updatePreviewCanBeExpandedAndCollapsedFromLocalDetail() {
        connect()
        val c = SelfTestComic().also { comic = it; it.create() }
        updateFixtureInstalled = true
        execUpdateFixture("""
            import server.manga_api as _qa_api
            _qa_api._qa_original_update_check = _qa_api._manga_check_one
            def _qa_update_check(source, comic_id, force_refresh=False):
                if comic_id == '${c.comicIdValue}':
                    return {'ok': True, 'has_update': True, 'missing_count': 1,
                            'latest': '第42话', 'missing': [{'id': 'qa-new-42', 'name': '第42话'}]}
                return _qa_api._qa_original_update_check(source, comic_id, force_refresh=force_refresh)
            _qa_api._manga_check_one = _qa_update_check
        """.trimIndent())
        rule.openCachedShelf()
        val tag = "manga_cached:${c.sourceKey}:${c.comicIdValue}"
        rule.waitUntil(60_000) { rule.onAllNodesWithTag(tag).fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag(tag).performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("manga_detail_list").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("check_update"))
        rule.onNodeWithTag("check_update").performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("update_preview").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("update_preview"))
        rule.onNodeWithTag("update_preview").performClick()
        rule.onNodeWithText("第42话", substring = false).assertIsDisplayed()
        rule.onNodeWithTag("update_preview").performClick()
        assertTrue(rule.onAllNodesWithText("第42话", substring = false).fetchSemanticsNodes().isEmpty())
        rule.onNodeWithText("下载缺失章节（1）").assertIsDisplayed()
    }

    @Test fun chapterFinderOpensChosenChapterInsteadOfPreviousProgress() =
        verifyChapterFinderSelection(horizontalPaging = false)

    @Test fun chapterFinderOpensChosenChapterInPagedMode() =
        verifyChapterFinderSelection(horizontalPaging = true)

    private fun verifyChapterFinderSelection(horizontalPaging: Boolean) {
        val oldPrefs = ReaderPrefs.load(rule.activity)
        try {
            ReaderPrefs.save(rule.activity, oldPrefs.copy(horizontalPaging = horizontalPaging))
            checkChapterFinderSelection()
        } finally {
            ReaderPrefs.save(rule.activity, oldPrefs)
        }
    }

    private fun checkChapterFinderSelection() {
        connect()
        val c = SelfTestComic(resumeAtChapter = 1).also { comic = it; it.create() }
        rule.openCachedShelf()
        val tag = "manga_cached:${c.sourceKey}:${c.comicIdValue}"
        rule.waitUntil(60_000) { rule.onAllNodesWithTag(tag).fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag(tag).performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("manga_detail_list").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(hasTestTag("open_chapter_finder"))
        rule.onNodeWithTag("open_chapter_finder").performClick()
        rule.onNodeWithTag("chapter_finder_query").performTextInput("2")
        rule.onNodeWithTag("find_chapter_${SelfTestComic.CH2_ID}").performClick()
        rule.waitUntil(30_000) {
            rule.onAllNodesWithTag("manga_pages").fetchSemanticsNodes().isNotEmpty() ||
                rule.onAllNodesWithTag("manga_pager").fetchSemanticsNodes().isNotEmpty()
        }
        val pageTag = if (rule.onAllNodesWithTag("manga_pages").fetchSemanticsNodes().isNotEmpty())
            "manga_pages" else "manga_pager"
        rule.onNodeWithTag(pageTag).performTouchInput {
            click(androidx.compose.ui.geometry.Offset(width / 2f, height / 2f))
        }
        rule.waitUntil(20_000) {
            rule.onAllNodesWithTag("reader_progress_label").fetchSemanticsNodes()
                .any { it.config.toString().contains("2 / 2 · 1/1页") }
        }
        rule.onNodeWithText("${SelfTestComic.TITLE} · ${SelfTestComic.CH2_NAME}", substring = true)
            .assertIsDisplayed()
    }

    @Test fun exploreSourcesScrollPastDuplicateBundledNovelSource() {
        connect()
        rule.waitUntil(60_000) { rule.onAllNodesWithText("浏览").fetchSemanticsNodes().isNotEmpty() }
        rule.onAllNodesWithText("浏览")[0].performClick()
        rule.onNodeWithText("探索（漫画分类 / 小说榜单）").performClick()
        rule.waitUntil(60_000) { rule.onAllNodesWithTag("explore_sources").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("explore_sources").performScrollToNode(hasText("精华书阁"))
        rule.onNodeWithText("精华书阁").assertIsDisplayed()
        rule.onNodeWithText("精华书阁").performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithText("精华书阁 ·", substring = true).fetchSemanticsNodes().isNotEmpty() }
        println("RELEASE203 explore_duplicate_source_navigation_passed")
    }

    @Test fun novelDetailActionsRemainReadableOnPhone() {
        connect()
        book = SelfTestBook("selftest_release203_layout").also { it.create() }
        rule.openCachedShelf()
        rule.waitUntil(60_000) { rule.onAllNodesWithText(SelfTestBook.BOOK_NAME).fetchSemanticsNodes().isNotEmpty() }
        rule.onAllNodesWithText(SelfTestBook.BOOK_NAME)[0].performClick()
        rule.waitUntil(30_000) { rule.onAllNodesWithText("导出 TXT").fetchSemanticsNodes().isNotEmpty() }
        for (label in listOf("导出 TXT", "从书架删除", "检查更新", "净化正文")) {
            val node = rule.onNodeWithText(label)
            node.assertIsDisplayed()
            val bounds = node.getUnclippedBoundsInRoot()
            assertTrue("$label 被挤成竖排：$bounds",
                (bounds.right - bounds.left).value > 120 && (bounds.bottom - bounds.top).value < 80)
        }
        println("RELEASE203 novel_detail_actions_two_column_readable")
    }

    @Test fun categorySourceTabsSwitchAndThemeButtonsWrap() {
        connect()
        val result = runBlocking { gateway.httpText(endpoint.port, "/api/explore/sources") }
        assertTrue(result.ok)
        val sources = EngineData.exploreMangaSources(result.body)
        val copy = sources.single { it.key == "copymanga" }
        val alternative = sources.first { it.key != copy.key && it.categories.isNotEmpty() }
        rule.waitUntil(60_000) { rule.onAllNodesWithText("浏览").fetchSemanticsNodes().isNotEmpty() }
        rule.onAllNodesWithText("浏览")[0].performClick()
        rule.onNodeWithText("探索（漫画分类 / 小说榜单）").performClick()
        rule.waitUntil(60_000) { rule.onAllNodesWithTag("category_source_copymanga").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("category_source_copymanga").performClick()
        val themes = copy.categories.filter { it.group == "题材" }.take(2)
        assertTrue(themes.size == 2)
        val bounds = themes.map { c ->
            val tag = "manga_cat_copymanga_" + c.url
            rule.onNodeWithTag("explore_sources").performScrollToNode(hasTestTag(tag))
            rule.onNodeWithTag(tag).getUnclippedBoundsInRoot()
        }
        assertTrue("分类按钮应同排显示而非全宽竖列", bounds[0].top == bounds[1].top)
        rule.onNodeWithTag("explore_sources").performScrollToNode(hasTestTag("category_source_" + alternative.key))
        rule.onNodeWithTag("category_source_" + alternative.key).performClick()
        val alternativeTag = "manga_cat_" + alternative.key + "_" + alternative.categories.first().url
        rule.onNodeWithTag("explore_sources").performScrollToNode(hasTestTag(alternativeTag))
        rule.onNodeWithTag(alternativeTag).assertIsDisplayed()
        println("RELEASE203 category_tabs_switch_grouped_buttons_wrap")
    }

    @Test fun singleSourceUsesGroupedButtonsAndCollapsesDiagnostics() {
        connect()
        val result = runBlocking { gateway.httpText(endpoint.port, "/api/explore/sources") }
        val copy = EngineData.exploreMangaSources(result.body).single { it.key == "copymanga" }
        rule.waitUntil(60_000) { rule.onAllNodesWithText("浏览").fetchSemanticsNodes().isNotEmpty() }
        rule.onAllNodesWithText("浏览")[0].performClick()
        rule.onNodeWithText("内置漫画源").performClick()
        rule.waitUntil(60_000) { rule.onAllNodesWithTag("manga_source_list").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithTag("manga_source_list").performScrollToNode(hasText("拷贝漫画"))
        rule.onNodeWithText("拷贝漫画").performClick()
        rule.waitUntil(60_000) { rule.onAllNodesWithTag("manga_source_page").fetchSemanticsNodes().isNotEmpty() }
        assertTrue("单源页面不得显示其他源切换按钮",
            rule.onAllNodesWithText("MangaDex").fetchSemanticsNodes().isEmpty())
        assertTrue(rule.onAllNodesWithText("依赖判定：", substring = true).fetchSemanticsNodes().isEmpty())
        val bounds = copy.categories.filter { it.group == "题材" }.take(2).map { category ->
            val tag = "src_cat_" + category.url
            rule.onNodeWithTag("manga_source_page").performScrollToNode(hasTestTag(tag))
            rule.onNodeWithTag(tag).getUnclippedBoundsInRoot()
        }
        assertTrue(bounds.size == 2 && bounds[0].top == bounds[1].top)
        rule.onNodeWithTag("manga_source_info_toggle").performClick()
        rule.onNodeWithTag("manga_source_page").performScrollToNode(hasText("依赖判定：", substring = true))
        rule.onNodeWithText("依赖判定：", substring = true).assertIsDisplayed()
        rule.onNodeWithTag("manga_source_info_toggle").performClick()
        InstrumentationRegistry.getInstrumentation().uiAutomation.performGlobalAction(
            android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_BACK)
        rule.waitUntil(30_000) { rule.onAllNodesWithTag("manga_source_list").fetchSemanticsNodes().isNotEmpty() }
        println("RELEASE203 single_source_grouped_diagnostics_collapsed")
    }
}
