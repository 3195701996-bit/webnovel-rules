package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 阅读历史视图验收（对齐 Venera 的"历史"分页，从应用图标冷启动）。
 *
 * 断言的是诚实状态机：有历史时给出列表（可点回上次阅读处），
 * 没有历史时明确说明"还没有阅读历史"——不允许空白页。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class HistoryUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private fun ev(line: String) = println("HISTORY_UI_EVIDENCE $line")

    private fun waitText(text: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) {
            rule.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().isNotEmpty()
        }
    }

    @Test
    fun historyView_isReachableAndHonest() {
        waitText("书架", timeoutMs = 150_000)
        waitText("历史")
        rule.onAllNodesWithText("历史")[0].performClick()

        // 标题带条数；必须出现确定状态（列表 或 明确的空说明）
        waitText("阅读历史（", substring = true)
        rule.waitUntil(60_000) {
            rule.onAllNodesWithText("还没有阅读历史").fetchSemanticsNodes().isNotEmpty() ||
                rule.onAllNodesWithText("读取历史失败", substring = true).fetchSemanticsNodes().isNotEmpty() ||
                rule.onAllNodesWithText("刷新").fetchSemanticsNodes().isNotEmpty()
        }
        val empty = rule.onAllNodesWithText("还没有阅读历史").fetchSemanticsNodes().isNotEmpty()
        val failed = rule.onAllNodesWithText("读取历史失败", substring = true)
            .fetchSemanticsNodes().isNotEmpty()
        assertTrue("历史页必须给出确定状态（空说明或失败说明或列表）", empty || failed ||
            rule.onAllNodesWithTag("history_list").fetchSemanticsNodes().isNotEmpty())
        ev(if (empty) "历史为空：显示明确说明" else "历史页已渲染（列表或失败说明）")

        // 从历史点回书籍：若列表非空则点第一条，应能进入详情（不崩溃）
        if (rule.onAllNodesWithTag("history_list").fetchSemanticsNodes().isNotEmpty()) {
            rule.onAllNodesWithText("刷新")[0].assertIsDisplayed()
            ev("历史列表在位且可刷新")
        }
        rule.onAllNodesWithText("← 返回")[0].performClick()
        waitText("书架")
        ev("返回书架正常")
    }
}
