package com.webnovel.mobile

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * **目标机回归包的入口自检**（路线 §7 P0-4 的配套）。
 *
 * 为什么需要它：`02-交付清单/手机阅读器-目标机回归包-<版本>.md` 是**给用户在真机上照着做**
 * 的文档，里面写了"点哪个按钮、进哪个页面"。一旦界面改了名（实测就撞过：
 * 文档写「小说搜索」，而浏览页上真正的入口叫「**搜小说**」），用户会卡在找不到入口，
 * 却以为是功能坏了——比不写文档更糟。
 *
 * 本用例把文档里出现的**全部入口**在真实界面上走一遍，标签/入口不存在就失败：
 *   · 底部四个页签：书架 / 浏览 / 下载 / 设置
 *   · 浏览页：搜小说、搜漫画
 *   · 设置页：产物基线（版本+revision）、引擎状态、Python 版本、
 *     存储管理、备份与恢复、书源管理（含「导出」/「导入」）、高级（网页诊断）
 *   · 书源管理页：src_export_all（导出）
 *   · 备份页：backup_scope_summary（范围数字）、创建备份按钮
 *   · 存储页：storage_total（总占用）、clear_regen（清理入口）
 *   · 下载页：全部暂停 / 全部继续，以及空队列时的说明
 *
 * 它**不**替代回归包本身（真机上的网络与系统行为仍要在你的手机上跑），
 * 只保证"照做即可"这句话成立：入口确实在那里、名字确实叫那个。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class AcceptanceEntryPointsTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private fun ev(line: String) = println("ENTRY_POINTS_EVIDENCE $line")

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size
    private fun texts(t: String, substring: Boolean = false) =
        rule.onAllNodesWithText(t, substring = substring).fetchSemanticsNodes().size

    private fun waitText(t: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) { texts(t, substring) > 0 }
    }

    /** 直接点（底部页签这类不在可滚动容器里的节点不能 performScrollTo，会抛错） */
    private fun clickText(t: String) {
        rule.onAllNodesWithText(t)[0].performClick()
    }

    /** 先滚到再点（设置页那种长列表里的行） */
    private fun clickRow(t: String) {
        rule.onAllNodesWithText(t)[0].performScrollTo().performClick()
    }

    private fun back() {
        rule.onAllNodesWithText("← 返回")[0].performClick()
    }

    @Test
    fun documentedEntryPointsExist() {
        // ── 底部页签（文档里全程要用） ──
        waitText("书架", timeoutMs = 150_000)
        for (tab in listOf("书架", "浏览", "下载", "设置")) {
            assertTrue("底部页签「$tab」必须存在（文档按这个名字指路）", texts(tab) > 0)
        }
        ev("底部页签：书架/浏览/下载/设置 都在位")

        // ── 浏览页：搜小说 / 搜漫画 ──
        clickText("浏览")
        waitText("搜小说")
        assertTrue("浏览页必须有「搜小说」（文档 C/D 条按此指路）", texts("搜小说") > 0)
        assertTrue("浏览页必须有「搜漫画」", texts("搜漫画") > 0)
        ev("浏览页：搜小说 / 搜漫画 在位")

        // ── 设置页：产物基线 + 引擎状态 + Python + 各入口 ──
        clickText("设置")
        waitText("高级 · 诊断", timeoutMs = 60_000)
        assertTrue("设置页必须显示产物基线（版本/源码 revision/构建时间）",
            nodes("build_info") > 0)
        val build = rule.onAllNodesWithTag("build_info")[0]
            .fetchSemanticsNode().config.toString()
        assertTrue("产物基线必须含版本号：$build", build.contains("版本"))
        assertTrue("产物基线必须含源码 revision：$build", build.contains("源码"))
        assertTrue("设置页必须显示引擎状态", texts("引擎状态", substring = true) > 0)
        assertTrue("设置页必须显示 Python 版本", texts("Python：3.13（内嵌）") > 0)
        ev("设置页：产物基线（版本+源码）、引擎状态、Python 版本在位")

        for (row in listOf("存储管理", "备份与恢复", "书源管理")) {
            assertTrue("设置页必须有「$row」入口（回归包 §3.I/J 按此指路）", texts(row) > 0)
        }
        assertTrue("设置页必须有「高级（网页诊断）」（文档 §5 导出诊断的指路）",
            texts("高级（网页诊断）") > 0)
        ev("设置页：存储管理/备份与恢复/书源管理/高级（网页诊断）都在位")

        // ── 存储页：总占用 + 清理入口 ──
        clickRow("存储管理")
        rule.waitUntil(60_000) { nodes("storage_total") > 0 }
        assertTrue("存储页必须有总占用（storage_total）", nodes("storage_total") > 0)
        assertTrue("存储页必须有「可再生数据」清理入口（clear_regen）",
            nodes("clear_regen") > 0)
        ev("存储页：总占用 + 清理入口在位")
        back()

        // ── 备份页：范围数字 + 创建备份 ──
        waitText("备份与恢复", timeoutMs = 60_000)
        clickRow("备份与恢复")
        rule.waitUntil(60_000) { nodes("backup_scope_summary") > 0 }
        assertTrue("备份页必须显示范围实测数字（backup_scope_summary）",
            nodes("backup_scope_summary") > 0)
        assertTrue("备份页必须有「创建备份」按钮",
            texts("创建备份", substring = true) > 0)
        assertTrue("备份页必须有「从备份恢复」按钮",
            texts("从备份恢复", substring = true) > 0)
        ev("备份页：范围数字 + 创建备份 + 从备份恢复在位")
        back()

        // ── 书源管理页：导出 / 导入 ──
        waitText("书源管理", timeoutMs = 60_000)
        clickRow("书源管理")
        rule.waitUntil(60_000) { nodes("src_export_all") > 0 }
        assertTrue("书源管理页必须有「导出」（src_export_all）", nodes("src_export_all") > 0)
        assertTrue("书源管理页必须有「导入」", texts("导入") > 0)
        ev("书源管理页：导出 / 导入在位")
        back()

        // ── 下载页：全局控制 + 队列空态说明 ──
        waitText("下载", timeoutMs = 60_000)
        clickText("下载")
        waitText("全部暂停", timeoutMs = 60_000)
        assertTrue("下载页必须有「全部暂停」", texts("全部暂停") > 0)
        assertTrue("下载页必须有「全部继续」", texts("全部继续") > 0)
        val empty = texts("当前没有下载任务") > 0
        val groups = listOf("进行中（", "已暂停 / 已停止（", "已结束（").any {
            texts(it, substring = true) > 0
        }
        assertTrue("下载页必须有确定状态：空队列说明或任务分组（文档 §3.H 按此指路）",
            empty || groups)
        ev("下载页：全部暂停/全部继续在位，队列状态=${if (empty) "空队列说明" else "任务分组"}")

        // 漫画源页的「重建图片缓存」入口由 JmImagePipelineTest 覆盖（避免重复走网络）
        Unit
    }
}
