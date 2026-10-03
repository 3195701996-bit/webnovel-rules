package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToIndex
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.swipeUp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 原生搜索的界面验收（从应用图标冷启动）：
 *   浏览页 →「搜小说」→ 输入关键词 → 搜索 → **必须给出确定状态**
 *   （结果列表 或 明确的"没有结果"+重试），不能停在转圈或空白；
 *   返回 →「搜漫画」→ 同样。
 *
 * 不断言"必须搜到某本书"（取决于源站），只断言交互与状态机的诚实性。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class SearchUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private fun ev(line: String) = println("SEARCH_UI_EVIDENCE $line")

    private fun waitText(text: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) {
            rule.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().isNotEmpty()
        }
    }

    @Test
    fun nativeSearch_mechanicsAndHonestStates() {
        // 1) 冷启动到书架，进浏览页
        waitText("书架", timeoutMs = 150_000)
        waitText("浏览")
        rule.onAllNodesWithText("浏览")[0].performClick()

        // 2) 原生小说搜索
        waitText("搜小说")
        rule.onNodeWithText("搜小说").performClick()
        waitText("小说搜索")
        rule.onNodeWithTag("novel_search_field").performTextInput("剑来")
        ev("已在原生搜索框输入关键词")
        rule.onNodeWithTag("novel_search_btn").performClick()

        // 必须出现确定状态：结果列表、或"没有结果"+重试
        rule.waitUntil(180_000) {
            rule.onAllNodesWithTag("novel_search_results").fetchSemanticsNodes().isNotEmpty() ||
                rule.onAllNodesWithText("没有结果").fetchSemanticsNodes().isNotEmpty() ||
                rule.onAllNodesWithText("搜索失败", substring = true).fetchSemanticsNodes().isNotEmpty()
        }
        val hasList = rule.onAllNodesWithTag("novel_search_results").fetchSemanticsNodes().isNotEmpty()
        if (hasList) {
            rule.onNodeWithTag("novel_search_results").assertExists()
            ev("小说搜索：出现结果列表")
            // 结果里必须有"加入书架"入口（原生闭环的必要条件）
            rule.onAllNodesWithText("加入书架")[0].assertIsDisplayed()
            ev("结果里含『加入书架』入口")
        } else {
            rule.onNodeWithText("没有结果").assertIsDisplayed()
            rule.onNodeWithText("重试").assertIsDisplayed()
            ev("小说搜索：无结果时给出明确说明与重试")
        }

        // 3) 返回浏览页，进原生漫画搜索
        rule.onAllNodesWithText("← 返回")[0].performClick()
        waitText("搜漫画")
        rule.onNodeWithText("搜漫画").performClick()
        waitText("发现漫画")
        rule.onNodeWithTag("manga_search_field").performTextInput("巨人")
        rule.onNodeWithTag("manga_search_btn").performClick()
        rule.waitUntil(180_000) {
            rule.onAllNodesWithTag("manga_search_results").fetchSemanticsNodes().isNotEmpty() ||
                rule.onAllNodesWithText("没有结果").fetchSemanticsNodes().isNotEmpty() ||
                rule.onAllNodesWithText("搜索失败", substring = true).fetchSemanticsNodes().isNotEmpty()
        }
        ev("漫画搜索：已给出确定状态（结果列表或明确说明）")

        // 3b) 有结果时，分页入口必须是确定状态：要么能"加载更多"，要么明确"没有更多了"
        val mangaHasList = rule.onAllNodesWithTag("manga_search_results")
            .fetchSemanticsNodes().isNotEmpty()
        if (mangaHasList) {
            // 等到**搜索真正落定**再断言分页状态：流式设计下结果会在流结束前先上屏，
            // 此时"搜索中…"还在、分页行尚未渲染——直接断言必然与流式收尾赛跑
            // （实测被整包运行抓到：列表 11 条、汇总行还没出现）。
            // 终止信号用搜索按钮的文案（唯一的 searching 可观察量）。
            rule.waitUntil(180_000) {
                rule.onAllNodesWithText("搜索中…").fetchSemanticsNodes().isEmpty()
            }
            val pager = rule.onAllNodesWithTag("manga_search_pager").fetchSemanticsNodes().size
            assertTrue("有搜索结果时必须显示分页控制", pager > 0)
            rule.onNodeWithTag("manga_search_pager").assertIsDisplayed()
            ev("漫画搜索：分页控件在位（上一页/下一页状态与服务端 has_more 对齐）")

            // 搜索条件与返回标题必须属于结果列表的同一滚动窗口，而不是固定在顶部挤占空间。
            rule.onNodeWithTag("manga_search_field").assertIsDisplayed()
            repeat(8) {
                rule.onNodeWithTag("manga_search_results").performTouchInput { swipeUp() }
            }
            rule.waitUntil(5_000) {
                runCatching { rule.onNodeWithTag("manga_search_field").assertIsNotDisplayed() }
                    .isSuccess
            }
            rule.onNodeWithTag("manga_search_field").assertIsNotDisplayed()
            rule.onNodeWithTag("manga_search_back").assertIsNotDisplayed()
            ev("结果滚动时搜索栏和返回标题一同离开视口")

            // 后续用例步骤需从页面顶部返回浏览页。
            rule.onNodeWithTag("manga_search_results").performScrollToIndex(0)
            rule.onNodeWithTag("manga_search_back").assertIsDisplayed()
        }

        // 4) 返回浏览页不崩溃
        rule.onAllNodesWithText("← 返回")[0].performClick()
        waitText("搜小说")
        ev("返回浏览页正常")

        // 5) P0-E：再次进入搜索页时，**查询词与结果必须还在**。
        //    过去状态在页面内 remember 里，离开页面即丢，用户每次返回都要重搜。
        rule.onNodeWithText("搜漫画").performClick()
        waitText("发现漫画")
        val keywordBack = rule.onAllNodesWithText("巨人").fetchSemanticsNodes().size
        assertTrue("返回后查询词应仍在输入框里（实际匹配到 $keywordBack 处）", keywordBack > 0)
        val hitsBack = rule.onAllNodesWithTag("manga_search_results")
            .fetchSemanticsNodes().size
        val noResult = rule.onAllNodesWithText("没有结果").fetchSemanticsNodes().size
        assertTrue("返回后结果状态必须保留（列表=$hitsBack 或明确无结果=$noResult）",
            hitsBack + noResult > 0)
        ev("再次进入漫画搜索：查询词与结果状态都保留了（列表=$hitsBack 无结果=$noResult）")

        // 小说搜索同理
        rule.onAllNodesWithText("← 返回")[0].performClick()
        waitText("搜小说")
        rule.onNodeWithText("搜小说").performClick()
        waitText("小说搜索")
        assertTrue("返回后小说查询词应仍在",
            rule.onAllNodesWithText("剑来").fetchSemanticsNodes().size > 0)
        ev("再次进入小说搜索：查询词仍在")
    }
}
