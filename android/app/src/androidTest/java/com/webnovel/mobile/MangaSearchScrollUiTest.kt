package com.webnovel.mobile

import androidx.activity.ComponentActivity
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performTextClearance
import androidx.compose.ui.test.swipeUp
import androidx.compose.ui.test.swipeDown
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import coil.ImageLoader
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** 用离线分页响应验收真实搜索 Compose：结果滚动时搜索条件和返回标题必须一起滚走。 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class MangaSearchScrollUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<ComponentActivity>()

    private class FixtureGateway : MangaSearchGateway {
        override suspend fun httpText(port: Int, path: String, auth: Boolean) =
            HttpText(200, if (path == "/api/manga/sources") {
                """{"sources":[{"key":"fixture","name":"离线测试源","status":"supported","category_label":"可浏览与搜索"}]}"""
            } else """{}""")

        override suspend fun httpPost(port: Int, path: String, json: String?, auth: Boolean) =
            HttpText(200, """{"ok":true}""")

        override suspend fun streamEvents(
            port: Int,
            path: String,
            readTimeoutMs: Int,
            onEvent: (JSONObject) -> Unit,
        ): Int {
            val groups = JSONArray()
            repeat(45) { index ->
                groups.put(JSONObject()
                    .put("title", "离线漫画 ${index + 1}")
                    .put("sources", JSONArray().put(JSONObject()
                        .put("source", "fixture")
                        .put("source_name", "离线测试源")
                        .put("id", "comic-${index + 1}"))))
            }
            onEvent(JSONObject()
                .put("groups", groups)
                .put("has_more", true)
                .put("total_hits", 45)
                .put("page", 1)
                .put("page_size", 30)
                .put("done", 1)
                .put("total", 1))
            onEvent(JSONObject().put("finished", true).put("groups", groups)
                .put("has_more", true).put("total_hits", 45).put("page", 1)
                .put("page_size", 30).put("done", 1).put("total", 1))
            return 200
        }
    }

    /** 让旧搜索在途，随后新搜索先完成，再故意送达已取消请求的迟到事件。 */
    private class InterleavedGateway : MangaSearchGateway {
        val oldStarted = CompletableDeferred<Unit>()
        val releaseOld = CompletableDeferred<Unit>()
        val oldDelivered = CompletableDeferred<Unit>()

        override suspend fun httpText(port: Int, path: String, auth: Boolean) =
            HttpText(200, if (path == "/api/manga/sources") {
                """{"sources":[{"key":"fixture","name":"离线测试源","status":"supported","category_label":"可浏览与搜索"}]}"""
            } else """{}""")

        override suspend fun httpPost(port: Int, path: String, json: String?, auth: Boolean) =
            HttpText(200, """{"ok":true}""")

        override suspend fun streamEvents(
            port: Int,
            path: String,
            readTimeoutMs: Int,
            onEvent: (JSONObject) -> Unit,
        ): Int {
            val old = path.contains(android.net.Uri.encode("旧查询"))
            if (old) {
                oldStarted.complete(Unit)
                // 模拟底层流暂时不理会取消；上层代际守卫仍必须阻止迟到回包改 UI。
                withContext(NonCancellable) { releaseOld.await() }
            }
            val title = if (old) "旧结果" else "新结果"
            val groups = JSONArray().put(JSONObject()
                .put("title", title)
                .put("sources", JSONArray().put(JSONObject()
                    .put("source", "fixture")
                    .put("source_name", "离线测试源")
                    .put("id", title))))
            onEvent(JSONObject().put("finished", true).put("groups", groups)
                .put("has_more", false).put("total_hits", 1).put("page", 1)
                .put("page_size", 30).put("done", 1).put("total", 1))
            if (old) oldDelivered.complete(Unit)
            return 200
        }
    }

    @Test
    fun searchHeaderScrollsAwayWithLongResults() {
        val gateway = FixtureGateway()
        val loader = ImageLoader.Builder(rule.activity).build()
        rule.setContent {
            MangaSearchScreen(
                gateway = gateway,
                ep = EngineEndpoint("offline-fixture", 1, ""),
                loader = loader,
                onOpen = {},
                onBack = {},
            )
        }

        rule.onNodeWithTag("manga_search_field").performTextInput("离线验收")
        rule.onNodeWithTag("manga_search_btn").performClick()
        rule.waitUntil(30_000) {
            rule.onAllNodesWithTag("manga_result_card").fetchSemanticsNodes().isNotEmpty()
        }

        val results = rule.onNodeWithTag("manga_search_results")
        val root = results.fetchSemanticsNode().boundsInRoot
        // Use repeated touch gestures, not semantics' instant scroll-to-item: this
        // validates the actual one-finger reader-style browsing path.
        repeat(5) { results.performTouchInput { swipeUp() } }
        rule.waitUntil(5_000) {
            val field = rule.onAllNodesWithTag("manga_search_field").fetchSemanticsNodes()
                .firstOrNull()?.boundsInRoot
            val back = rule.onAllNodesWithTag("manga_search_back").fetchSemanticsNodes()
                .firstOrNull()?.boundsInRoot
            field != null && back != null &&
                (field.bottom <= root.top || field.top >= root.bottom) &&
                (back.bottom <= root.top || back.top >= root.bottom)
        }
        val fieldBounds = rule.onNodeWithTag("manga_search_field").fetchSemanticsNode().boundsInRoot
        val backBounds = rule.onNodeWithTag("manga_search_back").fetchSemanticsNode().boundsInRoot
        assertTrue("滚动长结果后搜索框应移出屏幕可视区域: $fieldBounds / $root",
            fieldBounds.bottom <= root.top || fieldBounds.top >= root.bottom)
        assertTrue("滚动长结果后返回标题应移出屏幕可视区域: $backBounds / $root",
            backBounds.bottom <= root.top || backBounds.top >= root.bottom)

        repeat(6) { results.performTouchInput { swipeDown() } }
        rule.waitUntil(5_000) {
            rule.onAllNodesWithTag("manga_search_field").fetchSemanticsNodes().isNotEmpty()
        }
    }

    @Test
    fun lateOldSearchCannotReplaceNewResults() {
        val gateway = InterleavedGateway()
        val loader = ImageLoader.Builder(rule.activity).build()
        rule.setContent {
            MangaSearchScreen(
                gateway = gateway,
                ep = EngineEndpoint("offline-fixture", 1, ""),
                loader = loader,
                onOpen = {},
                onBack = {},
            )
        }
        val field = rule.onNodeWithTag("manga_search_field")
        field.performTextInput("旧查询")
        rule.onNodeWithTag("manga_search_btn").performClick()
        rule.waitUntil(5_000) { gateway.oldStarted.isCompleted }

        field.performTextClearance()
        field.performTextInput("新查询")
        rule.onNodeWithTag("manga_search_btn").performClick()
        rule.waitUntil(5_000) {
            rule.onAllNodesWithText("新结果").fetchSemanticsNodes().isNotEmpty()
        }

        gateway.releaseOld.complete(Unit)
        rule.waitUntil(5_000) { gateway.oldDelivered.isCompleted }
        rule.waitForIdle()
        rule.onNodeWithText("新结果").assertExists()
        rule.onNodeWithText("旧结果").assertDoesNotExist()
    }
}
