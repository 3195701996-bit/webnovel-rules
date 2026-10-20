package com.webnovel.mobile

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.assertTextContains
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assume.assumeTrue
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class CopyBrowseReleaseTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()

    @Test fun officialCategoryPagesAndVisiblePagination() = runBlocking {
        val testProxy = InstrumentationRegistry.getArguments().getString("testProxy")
        assumeTrue("Real-network test requires an explicit test proxy", !testProxy.isNullOrBlank())
        val gateway = EngineGateway(rule.activity.applicationContext)
        val ep = (gateway.connect() as EngineState.Ready).endpoint
        val previous = gateway.httpText(ep.port, "/api/net/proxy")
        assertTrue(previous.ok)
        val oldProxy = JSONObject(previous.body).getJSONObject("data").optString("proxy", "")
        try {
            assertTrue(gateway.httpPost(ep.port, "/api/net/proxy", JSONObject().put("proxy", testProxy).toString()).ok)
            val sources = gateway.httpText(ep.port, "/api/manga/sources")
            assertTrue(sources.ok)
            assertTrue(EngineData.sources(sources.body).none { it.key == "copymanga_web" })
            assertTrue(EngineData.sources(sources.body).any { it.key == "copymanga" })
            val catalog = gateway.httpText(ep.port, "/api/explore/sources")
            assertTrue(catalog.ok)
            assertTrue(EngineData.exploreMangaSources(catalog.body).single { it.key == "copymanga" }.categories.size == 70)
            val path = "/api/manga/browse?source=copymanga&category=theme%3Daiqing%26ordering%3D-datetime_updated&page="
            val first = gateway.httpText(ep.port, path + "1")
            val second = gateway.httpText(ep.port, path + "2")
            assertTrue("Page 1: ${first.code} ${first.body.take(80)}", first.ok)
            assertTrue("Page 2: ${second.code} ${second.body.take(80)}", second.ok)
            val a = EngineData.mangaSearch(first.body).first
            val b = EngineData.mangaSearch(second.body).first
            assertTrue(a.size == 30 && b.size == 30)
            assertTrue(a.none { x -> b.any { it.comicId == x.comicId } })
            rule.waitUntil(60_000) { rule.onAllNodesWithText("浏览").fetchSemanticsNodes().isNotEmpty() }
            rule.onAllNodesWithText("浏览")[0].performClick()
            rule.onNodeWithText("探索（漫画分类 / 小说榜单）").performClick()
            val tag = "manga_cat_copymanga_theme=aiqing&ordering=-datetime_updated"
            rule.waitUntil(30_000) { rule.onAllNodesWithTag("explore_sources").fetchSemanticsNodes().isNotEmpty() }
            rule.onNodeWithTag("explore_sources").performScrollToNode(androidx.compose.ui.test.hasTestTag(tag))
            rule.onNodeWithTag(tag).performClick()
            rule.waitUntil(30_000) { rule.onAllNodesWithText("30 部", substring = true).fetchSemanticsNodes().isNotEmpty() }
            rule.onNodeWithTag("manga_browse_list").performScrollToNode(hasText("加载更多"))
            rule.onNodeWithText("加载更多").performClick()
            rule.waitUntil(30_000) { rule.onAllNodesWithText("60 部", substring = true).fetchSemanticsNodes().isNotEmpty() }
            rule.onNodeWithTag("manga_browse_caption").assertTextContains("第 2 /", substring = true)
            rule.onNodeWithTag("browse_filters").performClick()
            rule.onNodeWithTag("browse_filter_region_0").performClick()
            rule.waitUntil(30_000) { rule.onAllNodesWithText("30 部", substring = true).fetchSemanticsNodes().isNotEmpty() }
            rule.onNodeWithTag("manga_browse_caption").assertTextContains("第 1", substring = true)
            rule.onNodeWithTag("browse_filters").performClick()
            rule.onNodeWithTag("browse_filter_ordering_popular").performClick()
            rule.waitUntil(30_000) { rule.onAllNodesWithText("30 部", substring = true).fetchSemanticsNodes().isNotEmpty() }
            rule.onNodeWithTag("manga_browse_caption").assertTextContains("第 1", substring = true)
            for (order in listOf("-datetime_updated", "datetime_updated", "-popular", "popular")) {
                val filtered = gateway.httpText(ep.port, path + "1&region=0&status=1&ordering=$order")
                assertTrue("Filter $order: ${filtered.code}", filtered.ok)
                assertTrue(JSONObject(filtered.body).getInt("total_hits") > 0)
                assertTrue(EngineData.mangaSearch(filtered.body).first.size == 30)
            }
            println("COPY_BROWSE_RELEASE source_hidden categories=70 page1=30 page2=30 distinct=true ui=60")
        } finally {
            assertTrue(gateway.httpPost(ep.port, "/api/net/proxy", JSONObject().put("proxy", oldProxy).toString()).ok)
        }
    }
}
