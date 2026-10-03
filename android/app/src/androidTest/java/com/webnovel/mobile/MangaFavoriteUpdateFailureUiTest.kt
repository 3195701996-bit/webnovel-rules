package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.net.URLEncoder
import java.util.UUID

/** Validate the visible partial-failure/retry path using an intentionally unknown, offline source. */
@RunWith(AndroidJUnit4::class)
class MangaFavoriteUpdateFailureUiTest {
    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private val source = "selftest-missing-source"
    private val comicId = "favorite-check-${UUID.randomUUID()}"
    private lateinit var gateway: EngineGateway
    private lateinit var endpoint: EngineEndpoint
    private var favoriteCreated = false

    @Before
    fun setUp() = runBlocking {
        gateway = EngineGateway(InstrumentationRegistry.getInstrumentation().targetContext)
        endpoint = (gateway.connect() as? EngineState.Ready)?.endpoint
            ?: throw AssertionError("本机引擎未就绪")
        val response = gateway.httpPost(endpoint.port, "/api/manga/favorites",
            JSONObject().put("source", source).put("comic_id", comicId)
                .put("title", "收藏检查失败态自检").toString())
        assertTrue("自检收藏创建失败：HTTP ${response.code} ${response.body}", response.ok)
        favoriteCreated = true
        MangaFavoriteStartupCheck.release(endpoint.instanceId)
        // MainActivity may have loaded the empty shelf before this fixture existed.
        // Recreate it so the normal startup-check path reads the new favorite and
        // publishes the failure state through the same UI observed by this test.
        rule.activityRule.scenario.recreate()
        Unit
    }

    @After
    fun tearDown() = runBlocking {
        if (::gateway.isInitialized && ::endpoint.isInitialized && favoriteCreated) {
            val encodedSource = URLEncoder.encode(source, "UTF-8")
            val encodedId = URLEncoder.encode(comicId, "UTF-8")
            val result = gateway.httpDelete(endpoint.port,
                "/api/manga/favorites/$encodedSource/$encodedId")
            assertTrue("自检收藏清理失败：HTTP ${result.code} ${result.body}", result.ok)
            MangaFavoriteStartupCheck.release(endpoint.instanceId)
        }
    }

    @Test
    fun partialFailureIsVisibleAndRetryableFromFavoritesShelf() {
        rule.waitUntil(60_000) {
            rule.onAllNodesWithText("书架").fetchSemanticsNodes().isNotEmpty()
        }
        rule.onAllNodesWithText("浏览").let { nodes ->
            nodes[nodes.fetchSemanticsNodes().lastIndex].performClick()
        }
        rule.onAllNodesWithText("书架").let { nodes ->
            nodes[nodes.fetchSemanticsNodes().lastIndex].performClick()
        }
        rule.waitUntil(30_000) {
            rule.onAllNodesWithText("收藏").fetchSemanticsNodes().isNotEmpty()
        }
        rule.onNodeWithText("收藏").performClick()

        rule.waitUntil(30_000) {
            rule.onAllNodesWithTag("favorite_update_status").fetchSemanticsNodes().isNotEmpty() &&
                rule.onAllNodesWithText("1 部失败", substring = true)
                    .fetchSemanticsNodes().isNotEmpty()
        }
        rule.onNodeWithTag("favorite_update_status").assertIsDisplayed()
        rule.onNodeWithText("重试检查").assertIsDisplayed()

        rule.onNodeWithTag("favorite_update_retry").performClick()
        rule.waitUntil(30_000) {
            rule.onAllNodesWithText("1 部失败", substring = true)
                .fetchSemanticsNodes().isNotEmpty()
        }
        assertTrue("失败结果应保留可读的重试提示",
            rule.onAllNodesWithTag("favorite_update_retry").fetchSemanticsNodes().isNotEmpty())
    }
}
