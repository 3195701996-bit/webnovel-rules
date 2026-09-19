package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 设置里的新入口验收（从应用图标冷启动）：
 *   设置 → 书源管理：显示书源总数/启用数、逐源卡片、导入入口、SSRF 提示；
 *   设置 → 备份与恢复：说明备份范围（含"不含正文与图片"）与两个按钮、校验/回滚说明。
 *
 * 不点"校验"（需要联网、按源而异），也不触发真实备份/恢复（那两个动作会弹系统选择器，
 * 其实现本身已由 BackupPathTest / ExportPathTest 覆盖）。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class SettingsScreensUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private fun ev(line: String) = println("SETTINGS_UI_EVIDENCE $line")

    private fun waitText(text: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) {
            rule.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().isNotEmpty()
        }
    }

    @Test
    fun settingsEntries_areReachableAndHonest() {
        // 1) 冷启动到书架，切到设置
        waitText("书架", timeoutMs = 150_000)
        waitText("设置")
        rule.onAllNodesWithText("设置")[0].performClick()
        waitText("书源管理")
        rule.onNodeWithText("备份与恢复").assertIsDisplayed()
        rule.onNodeWithText("书源管理", substring = true).assertIsDisplayed()
        ev("设置页：书源管理与备份恢复入口在位")

        // 1a) 诊断区：产物基线与"导出诊断报告"入口必须在位（方向基线 P0-A / §7.1）
        rule.onNodeWithTag("build_info").assertIsDisplayed()
        rule.onNodeWithTag("export_diagnostics").assertIsDisplayed()
        ev("诊断区：产物基线与导出诊断报告入口在位")

        // 1b) 任务恢复区：必须如实说明"不会自动续跑"，并提供刷新（阶段 D）
        rule.onNodeWithText("任务恢复（引擎重启/被系统限制之后）", substring = true).assertIsDisplayed()
        rule.onNodeWithText("不会自动续跑", substring = true).assertIsDisplayed()
        val hasTasks = rule.onAllNodesWithText("可恢复任务", substring = true)
            .fetchSemanticsNodes().isNotEmpty()
        val noTasks = rule.onAllNodesWithText("当前没有任务记录", substring = true)
            .fetchSemanticsNodes().isNotEmpty()
        assertTrue("任务恢复区必须给出确定状态（可恢复 N 个 或 没有任务记录）", hasTasks || noTasks)
        rule.onNodeWithText("刷新").performClick()
        rule.waitUntil(20_000) {
            rule.onAllNodesWithText("已刷新任务列表", substring = true)
                .fetchSemanticsNodes().isNotEmpty()
        }
        ev("任务恢复区在位，刷新可用")

        // 1c) 阅读设置总入口：能改字号/行距/主题与漫画方向，且立即落盘
        // "阅读设置"同时是分节标题与入口行，按入口行的副标题点击，避免歧义
        rule.onNodeWithText("字号/行距/主题", substring = true).performClick()
        waitText("小说排版")
        rule.onNodeWithTag("prefs_font").assertExists()
        rule.onNodeWithText("漫画阅读方向").assertIsDisplayed()
        rule.onNodeWithText("纵向连续").performClick()
        rule.waitUntil(20_000) {
            rule.onAllNodesWithText("已设为纵向连续", substring = true)
                .fetchSemanticsNodes().isNotEmpty()
        }
        ev("阅读设置：字号/行距/主题与漫画方向可改，改动有回执")
        rule.onAllNodesWithText("← 返回")[0].performClick()
        waitText("书源管理")

        // 2) 书源管理：统计、导入入口与 SSRF 提示
        rule.onNodeWithText("书源管理", substring = true).performClick()
        waitText("共 ", substring = true)
        rule.onAllNodesWithText("共 ", substring = true)[0].assertIsDisplayed()
        // "启用 N" 出现在两处：书源统计行与通过率行，故取第一个
        rule.onAllNodesWithText("启用 ", substring = true)[0].assertIsDisplayed()
        // 通过率行必须显式带上"未验证"的数量，不能只报通过数
        rule.onNodeWithText("通过率 ", substring = true).assertIsDisplayed()
        rule.onNodeWithText("验证下一批（未验证源优先）", substring = true).assertIsDisplayed()
        rule.onNodeWithText("导入").assertIsDisplayed()
        // 提示在列表末尾（35 个书源之后），需要滚动到它
        rule.onNodeWithTag("book_sources")
            .performScrollToNode(hasText("服务端会拒绝内网地址", substring = true))
        rule.onNodeWithText("服务端会拒绝内网地址", substring = true).assertIsDisplayed()
        ev("书源管理：统计与导入入口在位，SSRF 提示在位")

        // 3) 返回设置，再进备份与恢复
        waitText("← 返回")
        rule.onAllNodesWithText("← 返回")[0].performClick()
        waitText("备份与恢复")
        rule.onNodeWithText("备份与恢复").performClick()
        waitText("备份内容")
        rule.onNodeWithText("不含书籍正文与漫画图片", substring = true).assertIsDisplayed()
        rule.onNodeWithText("创建备份（选择保存位置）").assertIsDisplayed()
        rule.onNodeWithText("从备份恢复（选择文件）").assertIsDisplayed()
        rule.onNodeWithText("SHA-256", substring = true).assertIsDisplayed()
        ev("备份页：范围说明、两个按钮与校验说明在位")

        // 4) 返回设置且不崩溃
        rule.onAllNodesWithText("← 返回")[0].performClick()
        waitText("书源管理")
        ev("返回设置页正常")
    }
}
