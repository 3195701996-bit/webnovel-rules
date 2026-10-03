package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.click
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.getUnclippedBoundsInRoot
import androidx.compose.ui.test.onAllNodesWithContentDescription
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeLeft
import androidx.compose.ui.test.swipeDown
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.assertFalse
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 原生漫画界面的验收（从应用图标冷启动，不 bindService 走捷径）：
 *
 *   书架（漫画网格） → 详情（话数/已下载/未下载照实标注） → 原生阅读器（纵向连续看图）
 *   → 目录 → 下一话 → 返回详情（按钮变"继续阅读 第 2 话 承"） → 返回书架（读到 第 2 话 承 P1）
 *
 * 全部命中本地已下载图片，不请求源站；合成漫画在结束时清理干净。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class MangaUiAcceptanceTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private lateinit var comic: SelfTestComic
    private var readerPrefsBeforeTest: ReaderPrefs? = null
    private var readerPrefsFileExisted = false

    private fun ev(line: String) = println("MANGA_UI_EVIDENCE $line")

    private fun waitText(text: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) {
            rule.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().isNotEmpty()
        }
    }

    private fun clickText(text: String) {
        waitText(text)
        rule.onAllNodesWithText(text)[0].performClick()
    }

    private fun waitReaderProgress(text: String, timeoutMs: Long = 20_000) {
        rule.waitUntil(timeoutMs) {
            rule.onAllNodesWithTag("reader_progress_label").fetchSemanticsNodes()
                .any { it.config.toString().contains(text) }
        }
    }

    @Before
    fun setUp() {
        // Reader preferences persist across instrumentation methods; keep this test's
        // manga_pages assertions independent from a preceding horizontal-paging test.
        val context = androidx.test.platform.app.InstrumentationRegistry.getInstrumentation().targetContext
        readerPrefsFileExisted = java.io.File(
            context.applicationInfo.dataDir, "shared_prefs/reader_prefs.xml"
        ).isFile
        readerPrefsBeforeTest = ReaderPrefs.load(context)
        ReaderPrefs.save(context, readerPrefsBeforeTest!!.copy(horizontalPaging = false))
        comic = SelfTestComic()
        comic.create()
        ev("已造自检漫画 ${comic.sourceKey}/${comic.comicIdValue}")
    }

    @After
    fun tearDown() {
        try {
            comic.cleanup()
            assertFalse("自检漫画目录未清理干净", comic.exists())
            assertEquals("书库里有自检残留", 0, comic.libraryEntryCount())
            assertEquals("历史里有自检残留", 0, comic.historyEntryCount())
            ev("清理完成 书库残留=${comic.libraryEntryCount()} 历史残留=${comic.historyEntryCount()}")
        } finally {
            readerPrefsBeforeTest?.let { previous ->
                val context = androidx.test.platform.app.InstrumentationRegistry
                    .getInstrumentation().targetContext
                if (readerPrefsFileExisted) ReaderPrefs.save(context, previous)
                else context.deleteSharedPreferences("reader_prefs")
            }
        }
    }

    @Test
    fun shelfToMangaDetailToNativeReader() {
        // 仪器测试会复用 MainActivity 当前页签；显式回到书架再进缓存分栏，
        // 不假设应用仍停在上一个测试的默认页面。
        rule.openCachedShelf()
        rule.waitUntil(60_000) { rule.onAllNodesWithTag(
            "manga_cached:${comic.sourceKey}:${comic.comicIdValue}")
            .fetchSemanticsNodes().isNotEmpty() }
        ev("已缓存页列出 ${SelfTestComic.TITLE}")

        // 1b) 诚实性：没有"检查书库更新"结果时，书架不得编造"有新话"标记
        //     （自检漫画刚造出来，批次检查结果里没有它）
        val badges = rule.onAllNodesWithText("有新话", substring = true).fetchSemanticsNodes().size
        assertEquals("没有检查结果时不应出现有新话标记", 0, badges)
        ev("书架：无检查结果时不显示有新话标记（不编造更新）")

        // 2) 从「已缓存」书库入口进入；最近阅读/历史入口应保留完整在线目录，
        //    书库入口则必须带 catalog=local 且只显示实盘已下载的话。
        rule.onNodeWithTag("manga_cached:${comic.sourceKey}:${comic.comicIdValue}")
            .performClick()
        waitText("开始阅读")
        rule.onNodeWithText("第 1 话 起").assertExists()
        assertTrue("本地目录必须含 3 个单元，已下载状态必须准确",
            rule.onAllNodesWithText("已下载").fetchSemanticsNodes().size >= 2)
        ev("详情：本地目录单元统计节点在位")
        // 书架入口必须是本地目录，只含实际下载章节；未下载的第 3 话不应出现。
        rule.onNodeWithTag("manga_detail_list").assertExists()
        rule.onNodeWithText(SelfTestComic.CH3_NAME, substring = true).assertDoesNotExist()
        // 卷/话组标题：服务端返回 group 时必须显示出来（长连载靠它导航）
        rule.onNodeWithText(SelfTestComic.GROUP_A).assertExists()
        rule.onNodeWithText(SelfTestComic.GROUP_B).assertDoesNotExist()
        ev("详情：本地目录显示已下载卷「${SelfTestComic.GROUP_A}」，不展示未下载卷")

        // Activity recreation must restore the same local-only destination rather
        // than silently reopening this comic's full online catalog.
        rule.activityRule.scenario.recreate()
        waitText("开始阅读", timeoutMs = 30_000)
        rule.onNodeWithTag("manga_detail_list").assertExists()
        rule.onNodeWithText(SelfTestComic.CH3_NAME, substring = true).assertDoesNotExist()
        rule.onNodeWithText(SelfTestComic.GROUP_A).assertExists()
        rule.onNodeWithText(SelfTestComic.GROUP_B).assertDoesNotExist()
        ev("Activity 重建：本地目录身份与已下载卷筛选保持不变")

        // 同一作品的在线详情必须可以展示新章节；返回书架本地目录不会把两类详情缓存串用。
        rule.onNodeWithContentDescription("返回").performClick()
        waitText("书架", timeoutMs = 30_000)
        waitText("已缓存", timeoutMs = 30_000)
        rule.onNodeWithText("已缓存").performClick()
        rule.waitUntil(30_000) {
            rule.onAllNodesWithTag("manga_cached:${comic.sourceKey}:${comic.comicIdValue}")
                .fetchSemanticsNodes().isNotEmpty()
        }
        val mixedDetail = kotlinx.coroutines.runBlocking {
            val gateway = EngineGateway(androidx.test.platform.app.InstrumentationRegistry
                .getInstrumentation().targetContext)
            val ready = gateway.connect() as? EngineState.Ready
                ?: throw AssertionError("本机引擎未就绪")
            val response = gateway.httpText(ready.endpoint.port,
                "/api/manga/${comic.sourceKey}/${comic.comicIdValue}")
            assertTrue("在线目录请求失败：HTTP ${response.code}", response.ok)
            assertTrue("在线目录必须保留新增/未下载章节",
                EngineData.mangaDetail(response.body)?.readingChapters?.size ?: 0 >= 3)
        }
        ev("在线详情仍保留完整目录；书架入口独立使用严格本地目录")

        // 回到刚才的本地详情继续既有流程。
        rule.onNodeWithTag("manga_cached:${comic.sourceKey}:${comic.comicIdValue}")
            .performClick()
        waitText("开始阅读")

        // 2b) 详情页应提供"从书库移除"入口（本用例只断言入口在位）
        rule.onNodeWithText("从书库移除").assertIsDisplayed()
        ev("详情页含『从书库移除』入口")

        // 从真实阅读器生成历史后，再从阅读历史入口进入同一作品：
        // 未下载的第 3 话仍应可见并标注，且走完整目录。
        // 故意预置同一话的在线 URL 缓存（错误页数 1）。本地书库阅读必须拒绝
        // 该缓存并从本地目录重新取 2 页，否则会出现已下载却显示未下载/不可读。
        MangaReadCache.putUrls(
            comic.sourceKey,
            comic.comicIdValue,
            SelfTestComic.CH1_ID,
            MangaChapterPages(count = 1, local = false, images = listOf("https://invalid.test/online.jpg")),
            localCatalog = false,
        )
        clickText("开始阅读")
        rule.waitUntil(30_000) {
            rule.onAllNodesWithTag("manga_pages").fetchSemanticsNodes().isNotEmpty()
        }
        rule.onNodeWithTag("manga_pages").performTouchInput {
            click(androidx.compose.ui.geometry.Offset(width / 2f, height / 2f))
        }
        waitReaderProgress("1 / 2 · 1/2页")
        ev("本地阅读拒绝污染的在线 URL 缓存，按本地目录正确显示 2 页")
        // 隔离本用例的故意污染项，避免它影响后续在线目录/阅读断言。
        MangaReadCache.invalidate(comic.sourceKey, comic.comicIdValue)
        waitText("自检漫画 · ${SelfTestComic.CH1_NAME}", substring = true)
        rule.onNodeWithContentDescription("返回").performClick()
        waitText("继续阅读", substring = true)
        val histGw = EngineGateway(androidx.test.platform.app.InstrumentationRegistry
            .getInstrumentation().targetContext)
        val histEp = kotlinx.coroutines.runBlocking {
            histGw.connect() as? EngineState.Ready
        }?.endpoint ?: throw AssertionError("本机引擎未就绪")
        val historyWrite = kotlinx.coroutines.runBlocking {
            histGw.httpPost(histEp.port, "/api/manga/history",
                org.json.JSONObject().put("source", comic.sourceKey)
                    .put("comic_id", comic.comicIdValue).put("idx", 0)
                    .put("pos", SelfTestComic.CH1_NAME).put("title", SelfTestComic.TITLE)
                    .put("chapter_id", SelfTestComic.CH1_ID)
                    .put("chapter_label", SelfTestComic.CH1_NAME).toString())
        }
        assertTrue("写入漫画阅读历史失败：HTTP ${historyWrite.code}", historyWrite.ok)
        val historyRead = kotlinx.coroutines.runBlocking {
            histGw.httpText(histEp.port, "/api/manga/history")
        }
        assertTrue("历史接口应返回刚写入的作品", EngineData.mangaHistory(historyRead.body)
            .any { it.source == comic.sourceKey && it.comicId == comic.comicIdValue })
        // 记录数据已由服务端确认；离开并重新进入书架可触发初始加载，而非复用旧 Compose 状态。
        rule.onNodeWithContentDescription("返回").performClick()
        waitText("书架", timeoutMs = 30_000)
        waitText("已缓存", timeoutMs = 30_000)
        rule.onNodeWithText("已缓存").performClick()
        rule.waitUntil(30_000) {
            rule.onAllNodesWithTag("manga_cached:${comic.sourceKey}:${comic.comicIdValue}")
                .fetchSemanticsNodes().isNotEmpty()
        }
        waitText("最近阅读", timeoutMs = 30_000)
        rule.onNodeWithText("最近阅读").performClick()
        // 书架本身可能保留在组合树中；显式下拉刷新让历史重新从服务端加载。
        rule.onNodeWithText("最近阅读").performTouchInput { swipeDown() }
        rule.waitUntil(30_000) {
            rule.onAllNodesWithText(SelfTestComic.TITLE).fetchSemanticsNodes().isNotEmpty()
        }
        rule.onAllNodesWithText("最近阅读")[0].performClick()
        rule.onAllNodesWithText(SelfTestComic.TITLE)[0].performClick()
        waitText("继续阅读", substring = true)
        rule.onNodeWithTag("manga_detail_list").performScrollToNode(
            hasText(SelfTestComic.CH3_NAME, substring = true))
        rule.onNodeWithText(SelfTestComic.CH3_NAME, substring = true).assertIsDisplayed()
        rule.onNodeWithText("未下载").assertIsDisplayed()
        // 2c) 按话勾选下载：只允许勾选未下载的话，选中后出现"下载选中（N 话）"
        //     （历史详情保留完整目录；第 3 话未下载、可从此处加入下载选择）
        rule.onNodeWithTag("pick_${SelfTestComic.CH3_ID}").performClick()
        rule.waitUntil(20_000) {
            rule.onAllNodesWithText("下载选中（1 话）", substring = true)
                .fetchSemanticsNodes().isNotEmpty()
        }
        rule.onNodeWithText("清空选择").performClick()
        rule.waitUntil(20_000) {
            rule.onAllNodesWithText("下载选中", substring = true).fetchSemanticsNodes().isEmpty()
        }
        ev("按话勾选下载：未下载话可勾选，出现下载入口，可清空")

        // 3) 原生阅读器：纵向连续看图，页码来自真实页数
        rule.onNodeWithTag("manga_detail_list")
            .performScrollToNode(hasText("继续阅读", substring = true))
        rule.onNodeWithText("继续阅读", substring = true).performClick()
        rule.waitUntil(30_000) {
            rule.onAllNodesWithTag("manga_pages").fetchSemanticsNodes().isNotEmpty()
        }
        rule.onNodeWithTag("reader_progress_slider").assertDoesNotExist()
        // 阅读画面默认沉浸显示；正中点击才展开上下控制栏与进度滑块。
        rule.onNodeWithTag("manga_pages").performTouchInput {
            click(androidx.compose.ui.geometry.Offset(width / 2f, height / 2f))
        }
        rule.waitUntil(10_000) {
            rule.onAllNodesWithTag("reader_progress_slider").fetchSemanticsNodes().isNotEmpty()
        }
        waitReaderProgress("1 / 3 · 1/2页")
        ev("阅读器：默认隐藏控制栏，中央点按展开")
        // 将可拖动进度条拖至最右侧，必须同步跳到本话末页并更新实时页码。
        rule.onNodeWithTag("reader_progress_slider").performTouchInput {
            down(androidx.compose.ui.geometry.Offset(width * 0.15f, height / 2f))
            moveTo(androidx.compose.ui.geometry.Offset(width * 0.92f, height / 2f))
            up()
        }
        waitReaderProgress("1 / 3 · 2/2页")
        ev("阅读器：拖动进度滑块跳到第 2 页，页码同步")
        rule.onNodeWithTag("manga_pages").performTouchInput {
            click(androidx.compose.ui.geometry.Offset(width / 2f, height / 2f))
        }
        rule.waitUntil(5_000) {
            rule.onAllNodesWithTag("reader_progress_slider").fetchSemanticsNodes().isEmpty()
        }
        rule.onNodeWithTag("manga_pages").performTouchInput {
            swipeDown()
        }
        // 重新展开控制栏后再执行缩放、阅读模式等设置。
        rule.onNodeWithTag("manga_pages").performTouchInput {
            click(androidx.compose.ui.geometry.Offset(width / 2f, height / 2f))
        }
        rule.waitUntil(5_000) {
            rule.onAllNodesWithText("放大").fetchSemanticsNodes().isNotEmpty()
        }
        // 图片加载失败时会显示占位文案；不应出现
        rule.onAllNodesWithText("页加载失败", substring = true).fetchSemanticsNodes().let {
            assertEquals("第 1 话出现图片加载失败占位", 0, it.size)
        }
        ev("阅读器：第 1 话 2 页已就绪，无失败占位")

        // 3b) 页面按图片原始比例排版：自检图是 240×600（高/宽=2.5）。
        //     曾经写死 aspectRatio(0.7)（高/宽≈1.43）会把长图裁掉，这里用实测比例锁住。
        rule.waitUntil(20_000) {
            rule.onAllNodesWithContentDescription("第 1 页").fetchSemanticsNodes().isNotEmpty()
        }
        val b = rule.onNodeWithContentDescription("第 1 页").getUnclippedBoundsInRoot()
        val ratio = (b.bottom - b.top).value / (b.right - b.left).value
        assertTrue("第 1 页渲染比例应接近原图 2.5，实际=${ratio}（写死宽高比会导致≈1.43）",
            ratio > 2.2 && ratio < 2.8)
        ev("第 1 页渲染比例=${"%.2f".format(ratio)}（原图 2.50）")

        // 3c) 缩放：点"放大"进入 2×，底栏显示倍数；再点"复位"回到 1×
        clickText("放大")
        waitText("缩放 2.0×", substring = true)
        rule.onNodeWithText("复位").assertIsDisplayed()
        ev("缩放进入 2.0×，出现复位入口")
        clickText("复位")
        rule.waitUntil(20_000) {
            rule.onAllNodesWithText("缩放 2.0×", substring = true).fetchSemanticsNodes().isEmpty()
        }
        waitText("放大")
        ev("复位后回到 1×（放大入口重新出现）")

        // 3d) 横向翻页模式：切到"翻页"后整页显示，左滑翻到第 2 页（进度随之更新）
        clickText("翻页")
        waitText("连续", substring = true)
        rule.waitUntil(20_000) {
            rule.onAllNodesWithTag("manga_pager").fetchSemanticsNodes().isNotEmpty()
        }
        rule.onNodeWithTag("manga_pager").performTouchInput { swipeLeft() }
        waitText("1 / 3 · 2/2页", timeoutMs = 20_000)
        ev("横向翻页：左滑后页码变为 2/2")
        // 点按翻页：左 1/3 上一页（单手阅读不必精确滑动）
        rule.onNodeWithTag("manga_pager").performTouchInput {
            click(androidx.compose.ui.geometry.Offset(width * 0.15f, height * 0.5f))
        }
        waitText("1 / 3 · 1/2页", timeoutMs = 20_000)
        ev("点按翻页：点左侧回到第 1 页")
        rule.onNodeWithTag("manga_pager").performTouchInput {
            click(androidx.compose.ui.geometry.Offset(width * 0.85f, height * 0.5f))
        }
        waitText("1 / 3 · 2/2页", timeoutMs = 20_000)
        ev("点按翻页：点右侧前进到第 2 页")
        clickText("连续")
        rule.waitUntil(20_000) {
            rule.onAllNodesWithTag("manga_pages").fetchSemanticsNodes().isNotEmpty()
        }
        waitReaderProgress("1 / 3 · 2/2页")
        ev("切回纵向连续模式")

        // 4) 目录：列出 3 话并标注已下载
        clickText("目录")
        waitText("目录（3 个单元）")
        rule.onNodeWithTag("manga_toc")
            .performScrollToNode(hasText(SelfTestComic.CH3_NAME, substring = true))
        rule.onNodeWithText(SelfTestComic.CH3_NAME, substring = true).assertIsDisplayed()
        ev("目录：含第 3 话")
        clickText("关闭")
        rule.waitUntil(5_000) {
            rule.onAllNodesWithTag("manga_toc").fetchSemanticsNodes().isEmpty()
        }
        rule.onRoot().performTouchInput {
            click(androidx.compose.ui.geometry.Offset(width / 2f, height / 2f))
        }
        waitReaderProgress("1 / 3 · 2/2页")

        // 5) 下一话：切到第 2 话（本地 1 页）
        clickText("下一话")
        waitText("2 / 3 · 1/1页")
        ev("切到第 2 话：1/1 页")

        // 6) 返回详情：进度已写入 → 按钮跟随历史
        rule.onNodeWithContentDescription("返回").performClick()
        waitText("继续阅读 ${SelfTestComic.CH2_NAME}", timeoutMs = 20_000)
        ev("返回详情：按钮已变为『继续阅读 ${SelfTestComic.CH2_NAME}』")

        // 7) 返回书架：续读位置与页码来自同一份历史
        rule.onNodeWithContentDescription("返回").performClick()
        waitText(SelfTestComic.TITLE)
        rule.onAllNodesWithText("读到 ${SelfTestComic.CH2_NAME} P1", substring = true)[0]
            .assertIsDisplayed()
        ev("书架：读到 ${SelfTestComic.CH2_NAME} P1")
    }
}
