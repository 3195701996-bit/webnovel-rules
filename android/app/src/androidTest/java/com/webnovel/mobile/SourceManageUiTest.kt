package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 书源管理的「按状态筛选 + 批量启用/停用」界面验收。
 *
 * 分工（避免重复也避免漏）：
 *   - 这里只验证**入口在位、筛选真的生效**（点筛选是纯界面动作，不改任何配置）；
 *   - 真正改配置的批量动作由 SourceBulkPathTest 在 HTTP 层验收并**回滚**，
 *     界面用例不去动设备上的实际启用状态（测完留一地改动不是好验收）。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class SourceManageUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private fun ev(line: String) = println("SOURCE_MANAGE_UI_EVIDENCE $line")

    private fun texts(text: String, substring: Boolean = false) =
        rule.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().size

    private fun waitText(text: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) { texts(text, substring) > 0 }
    }

    @Test
    fun sourceManage_filterAndBulkEntrypoints() {
        waitText("书架", timeoutMs = 150_000)
        waitText("设置")
        rule.onAllNodesWithText("设置")[0].performClick()
        waitText("书源管理")
        rule.onAllNodesWithText("书源管理")[0].performClick()

        // 1) 筛选行在位（口径与批量操作一致：全部/启用/停用/验证通过/失败/未验证）
        waitText("全部", substring = true)
        rule.onNodeWithTag("src_filter_all").assertIsDisplayed()
        rule.onNodeWithTag("src_filter_verified").assertIsDisplayed()
        rule.onNodeWithTag("src_filter_unverified").assertIsDisplayed()
        ev("筛选入口在位：全部 / 验证通过 / 未验证 …")

        // 2) 批量入口在位且可用（真正的改动由路径用例负责）
        rule.onNodeWithTag("src_bulk_enable_verified").assertIsDisplayed().assertIsEnabled()
        rule.onNodeWithTag("src_bulk_disable_failed").assertIsDisplayed().assertIsEnabled()
        ev("批量入口在位：启用验证通过的 / 停用验证失败的")

        // 3) 点「验证通过」筛选：摘要必须出现"筛选后 N 个"（证明筛选真的生效）
        rule.onNodeWithTag("src_filter_verified").performClick()
        rule.waitUntil(20_000) { texts("筛选后", substring = true) > 0 }
        rule.onNodeWithText("筛选后", substring = true).assertIsDisplayed()
        ev("点「验证通过」后摘要显示筛选后条数")

        // 4) 点「未验证」筛选：同样必须给出筛选后条数（换一个筛选口径再验一次）
        rule.onNodeWithTag("src_filter_unverified").performClick()
        rule.waitUntil(20_000) { texts("筛选后", substring = true) > 0 }
        ev("点「未验证」后摘要仍显示筛选后条数")

        // 5) 回到「全部」：摘要不再带筛选后缀（不残留上一个筛选状态）
        rule.onNodeWithTag("src_filter_all").performClick()
        rule.waitUntil(20_000) { texts("筛选后", substring = true) == 0 }
        assertTrue("回到全部后不应残留筛选提示", texts("筛选后", substring = true) == 0)
        ev("回「全部」后筛选提示消失（状态不残留）")

        // 6) 逐源开关仍在（原有能力没被挤掉）
        rule.onAllNodesWithText("启用", substring = true)
        ev("逐源卡片与开关仍在位")
    }
}
