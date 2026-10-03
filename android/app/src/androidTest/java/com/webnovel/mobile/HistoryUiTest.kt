package com.webnovel.mobile

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** 最近阅读已整合到书架页签；以本地自检条目验证历史会出现且可恢复刷新。 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class HistoryUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private lateinit var comic: SelfTestComic
    private lateinit var gateway: EngineGateway
    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext

    private fun texts(text: String) =
        rule.onAllNodesWithText(text, substring = true).fetchSemanticsNodes().size

    private fun waitText(text: String, timeoutMs: Long = 60_000) {
        rule.waitUntil(timeoutMs) { texts(text) > 0 }
    }

    @Before
    fun setUp() {
        comic = SelfTestComic(comicId = "__history_ui__")
        comic.create()
        gateway = EngineGateway(ctx)
        val endpoint = runBlocking {
            val state = gateway.connect()
            assertTrue("本机引擎应就绪：$state", state is EngineState.Ready)
            gateway.currentEndpoint()
        } ?: throw AssertionError("本机引擎未返回 endpoint")
        val saved = runBlocking {
            gateway.httpPost(endpoint.port, "/api/manga/history",
                JSONObject().put("source", comic.sourceKey)
                    .put("comic_id", comic.comicIdValue)
                    .put("idx", 1).put("pos", SelfTestComic.CH2_NAME)
                    .put("title", SelfTestComic.TITLE).toString())
        }
        assertTrue("自检阅读历史写入失败：HTTP ${saved.code}", saved.ok)
    }

    @After
    fun tearDown() {
        comic.cleanup()
        assertTrue("自检漫画未清理干净", !comic.exists())
        assertTrue("自检阅读记录未清理干净", comic.historyEntryCount() == 0)
    }

    @Test
    fun recentReadingShowsRecordedComicAfterReturningToShelf() {
        waitText("书架", timeoutMs = 150_000)
        // 强制离开再回来，触发书架按服务端最新历史重新加载，避免依赖启动时序。
        rule.onAllNodesWithText("浏览")[0].performClick()
        waitText("漫画（优先）")
        rule.onAllNodesWithText("书架")[0].performClick()
        waitText("最近阅读")
        rule.waitUntil(30_000) { texts(SelfTestComic.TITLE) > 0 }
        rule.onAllNodesWithText("最近阅读")[0].performClick()
        waitText(SelfTestComic.TITLE)
        assertTrue("最近阅读页应显示历史记录中的作品", texts(SelfTestComic.TITLE) > 0)
    }
}
