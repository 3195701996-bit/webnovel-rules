package com.webnovel.mobile

import androidx.compose.ui.semantics.getOrNull
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * **失败现场的一键自检**（方向基线 §7.1：开发侧负责把故障钉到具体一层）。
 *
 * 用户看到"搜不到/失败了"的地方必须有自助路径：点一下就把逐源四阶段实测跑完
 * （搜索 → 详情/目录 → 章节图片 → 真实取一张图），并把结论显示出来 + 指路导出诊断。
 * 本用例验证这条路径**真的能跑通并给出可读结论**（不是只放了个按钮）。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class MangaSelfCheckUiTest {

    @get:Rule(order = 1)
    val rule = createAndroidComposeRule<MainActivity>()

    @get:Rule(order = 0)
    val netGuard = object : org.junit.rules.TestRule {
        override fun apply(base: org.junit.runners.model.Statement,
                           description: org.junit.runner.Description)
            : org.junit.runners.model.Statement = object : org.junit.runners.model.Statement() {
            override fun evaluate() {
                try {
                    base.evaluate()
                } finally {
                    Airplane.set(false)     // 任何情况下都不能把设备留在断网状态
                }
            }
        }
    }

    private fun ev(line: String) = println("SELFCHECK_EVIDENCE $line")

    private fun texts(text: String, substring: Boolean = false) =
        rule.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().size

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size

    private fun waitText(text: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) { texts(text, substring) > 0 }
    }

    @Test
    fun failureSurface_offersWorkingSelfCheck() {
        waitText("书架", timeoutMs = 150_000)
        rule.onAllNodesWithText("浏览")[0].performClick()
        waitText("漫画（优先）", timeoutMs = 60_000)
        rule.onAllNodesWithText("搜漫画")[0].performClick()
        waitText("漫画搜索", timeoutMs = 30_000)

        // 走到"没有结果/失败"这一块（自检入口就在那里）。
        // 为什么要断网：在线时总有源对任何关键词都能返回点东西，
        // "失败块"反而不稳定出现；断网会让所有源都失败 → 结论块必然出现
        // （也正是用户最需要自检的场景：搜不出来、不知道为什么）。
        assertTrue("切换断网失败", Airplane.set(true))
        // 关键词必须**不会命中搜索缓存**：服务端只缓存非空结果，而更早的用例搜过
        // "巨人"并把结果缓存了——整包运行时（引擎一直活着）离线也会命中缓存、
        // 照样出结果，失败块就不出现（实测被整包运行抓到）。
        // 这个关键词只有本用例用，且只在本用例断网时搜，缓存不可能命中。
        rule.onNodeWithTag("manga_search_field").performTextInput("zzz自检入口用例A2")
        rule.onNodeWithTag("manga_search_btn").performClick()
        rule.waitUntil(90_000) {
            nodes("manga_search_selfcheck") > 0
        }
        rule.onNodeWithTag("manga_search_selfcheck").assertExists()
        ev("失败现场出现「运行漫画源自检」入口")

        // 点一下：必须真的跑起来并给出结论（逐源四阶段实测）
        rule.onNodeWithTag("manga_search_selfcheck").performClick()
        rule.waitUntil(240_000) {
            val t = resultText()
            t.contains("可用") || t.contains("阶段：") || t.contains("缺运行依赖") ||
                t.contains("自检没有产出结果")
        }
        val out = resultText()
        ev("自检结论：${out.take(300)}")
        ev("（本次是断网状态下跑的自检：逐源应在 search 阶段如实失败，而不是假装可用）")
        assertTrue("自检结论里必须逐源给出结果或失败阶段", out.contains("·"))
        assertTrue("自检结论必须指路导出诊断报告（用户要能把证据发回来）",
            out.contains("导出诊断报告"))
    }

    private fun resultText(): String {
        val ns = rule.onAllNodesWithTag("manga_search_selfcheck_result").fetchSemanticsNodes()
        if (ns.isEmpty()) return ""
        val prop = androidx.compose.ui.semantics.SemanticsProperties.Text
        return ns.joinToString(" ") { n ->
            n.config.getOrNull(prop)?.joinToString("") { it.text } ?: ""
        }
    }
}
