package com.webnovel.mobile

import androidx.compose.ui.semantics.getOrNull
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * **分源搜索**接入原生 App（0.51.0 风险评估 §3 的 P0-2）。
 *
 * 路线指出的问题：服务端早就支持 `source`，但原生搜索页首搜/翻页/流式回退三条路径
 * 都没带；单源页的按钮打开的也是不带源的通用搜索 —— 用户说"分源搜索失效"是准确的。
 *
 * 本用例按路线的退出门槛验证：
 *   1. 搜索页有"全部源 / 单源"选择，并显示依赖判定与实测结论；
 *   2. 选中单源后，结果只来自该源（卡片来源标注 + 汇总行"源：X"），且"当前源"如实显示；
 *   3. 换源**清掉上一轮结果**（不许把上一源的结果留在屏幕上）；
 *   4. 支持排序的源（jm）才出现排序选择，排序随搜索一起透传；
 *   5. 从单源页「在「X」里搜漫画」进来时，源已预选（这正是以前漏掉的那条入口）。
 *
 * 说明：本轮设备网络下 jm 搜索返回 0 条（源站侧不可达），因此单源断言用 MangaDex
 * （本环境可用）证明"只出该源结果"，用 jm 证明"入口/排序/无结果口径"。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class PerSourceSearchUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private fun ev(line: String) = println("PER_SOURCE_EVIDENCE $line")

    private fun texts(t: String, substring: Boolean = false) =
        rule.onAllNodesWithText(t, substring = substring).fetchSemanticsNodes().size

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size

    private fun tagText(tag: String): String {
        val prop = androidx.compose.ui.semantics.SemanticsProperties.Text
        return rule.onAllNodesWithTag(tag).fetchSemanticsNodes().joinToString(" ") { n ->
            n.config.getOrNull(prop)?.joinToString("") { it.text } ?: ""
        }
    }

    private fun waitText(t: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) { texts(t, substring) > 0 }
    }

    private fun openSearchFromBrowse() {
        waitText("书架", timeoutMs = 150_000)
        rule.onAllNodesWithText("浏览")[0].performClick()
        waitText("漫画（优先）", timeoutMs = 60_000)
        rule.onAllNodesWithText("搜漫画")[0].performClick()
        waitText("漫画搜索", timeoutMs = 30_000)
        rule.waitUntil(60_000) { nodes("manga_src_all") > 0 }
    }

    @Test
    fun singleSourceSearch_onlyShowsThatSource_andSwitchingClearsResults() {
        openSearchFromBrowse()
        ev("搜索页出现源选择（全部源 + 各内置源）")

        // 1) 选 MangaDex（本环境可用）→ 当前源如实显示
        rule.onNodeWithTag("manga_src_mangadex").performClick()
        rule.waitUntil(30_000) { tagText("manga_src_note").contains("MangaDex") }
        val note = tagText("manga_src_note")
        ev("当前源提示：${note.take(120)}")
        assertTrue("必须显示依赖判定与实测结论：$note",
            note.contains("依赖判定") && note.contains("实测"))

        // 2) 搜索 → 结果只来自 MangaDex，且汇总行写明来源
        rule.onNodeWithTag("manga_search_field").performTextInput("巨人")
        rule.onNodeWithTag("manga_search_btn").performClick()
        rule.waitUntil(120_000) {
            nodes("manga_result_card") > 0 || texts("没有结果") > 0
        }
        val hasResults = nodes("manga_result_card") > 0
        ev("单源搜索：结果列表=$hasResults")
        assertTrue("MangaDex 单源搜索应有结果（本环境可用）", hasResults)
        assertTrue("汇总行必须写明本次的源（证明 source 参数透传了）",
            texts("源：MangaDex", substring = true) > 0)
        // 其它源的名字不得出现在结果里（串源的直接证据）
        for (other in listOf("禁漫天堂", "包子漫画", "拷贝漫画", "nhentai")) {
            assertTrue("选了 MangaDex 却出现其它源「$other」的结果 = 串源",
                texts(other) == 0)
        }
        ev("结果只来自 MangaDex（其它源名称未出现）")

        // 3) 换到 jm：上一轮结果必须被清掉，且出现排序选择（jm 支持排序）
        ev("换源前 chips：" + listOf("all", "mangadex", "jm", "baozi", "nhentai")
            .joinToString { "$it=${nodes("manga_src_$it")}" })
        // 源 chips 是横向滚动的：屏幕外的 chip 必须先滚进来再点，
        // 否则点击坐标落在可视区外、等于没点（实测踩到：点了 jm，当前源还是 MangaDex）
        rule.onNodeWithTag("manga_src_jm").performScrollTo().performClick()
        ev("点击 jm 后 note=「${tagText("manga_src_note").take(80)}」")
        rule.waitUntil(30_000) { tagText("manga_src_note").contains("禁漫") }
        // 清空是 recomposition 之后由副作用做的：这里**等它收敛**再断言，
        // 直接硬断言会与副作用赛跑（实测偶发失败）
        rule.waitUntil(30_000) { nodes("manga_result_card") == 0 }
        assertTrue("换源后不许留着上一源的结果卡片", nodes("manga_result_card") == 0)
        assertTrue("换源后也不该还挂着上一轮的汇总行",
            texts("已加载", substring = true) == 0)
        rule.waitUntil(30_000) { nodes("manga_order_mr") > 0 }
        ev("换源已清空结果，并出现排序选择（jm 支持排序）")

        // 4) 选"最新"排序 → 搜索一次（本轮 jm 返回 0 条，断言"诚实口径"）
        rule.onNodeWithTag("manga_order_mr").performClick()
        rule.onNodeWithTag("manga_search_btn").performClick()
        rule.waitUntil(120_000) {
            nodes("manga_result_card") > 0 || texts("没有结果") > 0 ||
                nodes("manga_search_errors") > 0
        }
        val empty = texts("没有结果") > 0
        ev("jm 单源搜索终态：无结果提示=$empty（本环境 jm 源站侧不可达，如实显示）")
        assertTrue("jm 单源搜索必须给出确定终态（结果/没有结果/逐源说明）: " +
            "results=${nodes("manga_result_card")} 没有结果=$empty " +
            "errors=${nodes("manga_search_errors")}",
            nodes("manga_result_card") > 0 || empty || nodes("manga_search_errors") > 0)
    }

    @Test
    fun sourcePageEntry_preselectsThatSource() {
        // 从单源页进入搜索：源必须已预选（以前这条入口打开的是"全部源"的通用搜索）
        waitText("书架", timeoutMs = 150_000)
        rule.onAllNodesWithText("浏览")[0].performClick()
        waitText("漫画（优先）", timeoutMs = 60_000)
        rule.onAllNodesWithText("内置漫画源", substring = true)[0].performClick()
        rule.waitUntil(120_000) { nodes("manga_source_list") > 0 }
        rule.waitUntil(60_000) { texts("MangaDex", substring = true) > 0 }
        rule.onAllNodesWithText("MangaDex", substring = true)[0].performClick()
        rule.waitUntil(120_000) { nodes("manga_source_page") > 0 }
        rule.waitUntil(60_000) { nodes("manga_search_in_source") > 0 }
        rule.onNodeWithTag("manga_search_in_source").performClick()
        waitText("漫画搜索", timeoutMs = 30_000)
        rule.waitUntil(60_000) { nodes("manga_src_all") > 0 }
        rule.waitUntil(30_000) { tagText("manga_src_note").contains("MangaDex") }
        ev("从单源页进入：源已预选 = ${tagText("manga_src_note").take(60)}")
        assertTrue("单源页入口必须带着源进来（而不是打开全部源搜索）",
            tagText("manga_src_note").contains("MangaDex"))
    }
}
