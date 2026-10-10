package com.webnovel.mobile

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.getUnclippedBoundsInRoot
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class Release203RegressionTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()
    private var comic: SelfTestComic? = null
    private var book: SelfTestBook? = null
    private lateinit var gateway: EngineGateway
    private lateinit var endpoint: EngineEndpoint

    private fun connect() {
        gateway = EngineGateway(rule.activity.applicationContext)
        endpoint = (runBlocking { gateway.connect() } as EngineState.Ready).endpoint
    }

    @After fun cleanup() {
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
        println("RELEASE203 single_source_grouped_diagnostics_collapsed")
    }
}
