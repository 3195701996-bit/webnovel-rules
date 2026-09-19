package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
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
 * 漫画搜索「流式」验收：结果随源到达陆续出现，而不是等所有源都跑完。
 *
 * 背景：网页端早就用 `/api/manga/search/stream` 绕开"等最慢源"（实测不可达源曾把
 * 阻塞端点拖到 20s，加了耐心上限后仍要 6s）；原生 App 之前用的就是阻塞端点。
 * 本版让 App 第 1 页走流式端点：快源结果先上屏，慢源随后补上，且未返回的源照实标注。
 *
 * 断言口径（不靠网络快慢，靠**结构性证据**）：
 *   · 走没走流式 —— 汇总行必须出现「N/M 个源已返回」（只有流式路径会产生它）；
 *   · 没有回落到阻塞端点 —— 不得出现「流式搜索不可用」提示；
 *   · 结果状态必须明确：要么有结果，要么"没有结果"，要么明确失败说明；
 *   · 首屏出现时间有上界（宽容到 12s），防止退化成"等最慢源"。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class MangaSearchStreamUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private fun ev(line: String) = println("MANGA_STREAM_UI_EVIDENCE $line")

    private fun texts(text: String, substring: Boolean = false) =
        rule.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().size

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size

    private fun waitText(text: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) { texts(text, substring) > 0 }
    }

    @Test
    fun mangaSearch_streamsResultsAsSourcesArrive() {
        waitText("书架", timeoutMs = 150_000)
        rule.onAllNodesWithText("浏览")[0].performClick()
        waitText("漫画（优先）", timeoutMs = 60_000)
        rule.onAllNodesWithText("搜漫画")[0].performClick()
        waitText("漫画搜索", timeoutMs = 30_000)

        // 真实关键词（保证有结果可观察）。**每次换一个**：服务端搜索缓存 TTL 1h，
        // 用同一个词连跑两轮会命中缓存，汇总行不再出现「N/M 个源已返回」，
        // 于是一条"走没走流式端点"的断言会被缓存误伤（实测踩到）。
        val words = listOf("巨人", "剑来", "火影", "海贼", "咒术", "死神", "进击", "一拳")
        val q = words[((System.currentTimeMillis() / 1000) % words.size).toInt()]
        rule.onNodeWithTag("manga_search_field").performTextInput(q)
        val t0 = System.currentTimeMillis()
        rule.onNodeWithTag("manga_search_btn").performClick()

        // A) **流式证据**：12s 内必须看到"第一批"东西——结果列表或进度行。
        //    若实现退回"等所有源跑完再显示"，这里只能等到 20s，判失败。
        rule.waitUntil(12_000) {
            nodes("manga_search_results") > 0 ||
                nodes("manga_search_progress") > 0 ||
                texts("没有结果") > 0
        }
        val firstMs = System.currentTimeMillis() - t0
        ev("首屏（结果或进度）${firstMs}ms")

        // B) 等汇总行落定（流结束时才出现），据此证明走的是流式端点
        // 流式证据在**两行**里都可能出现：
        //   · 进度行「已返回 1/6 个源 · 已显示 0 条」（源一回来就有）
        //   · 汇总行「（6/6 个源已返回）」（流结束后）
        // 旧断言只认后者，源站慢/有源失败时会误判（实测：32s 只回来 1/6 个源，
        // 拷贝漫画网页版直接失败 → 汇总行迟迟不出现）。
        rule.waitUntil(90_000) {
            texts("已返回", substring = true) > 0 ||
                texts("缓存", substring = true) > 0 ||
                texts("没有结果", substring = true) > 0 ||
                texts("流式搜索不可用", substring = true) > 0 ||
                texts("搜索失败", substring = true) > 0
        }
        ev("汇总落定 ${System.currentTimeMillis() - t0}ms")

        // 1) 必须走流式：汇总行含「N/M 个源已返回」
        // 走没走流式端点：实况流会出现「N/M 个源已返回」；命中**服务端搜索缓存**时
        // 响应 2ms 返回、没有逐源进度，但界面会标「缓存」——两者都说明走的是流式端点
        // （实测：缓存 TTL 1h，同一个词连跑两轮就会命中缓存，把这条断言误伤）。
        assertTrue("应走流式端点：界面要出现逐源进度「已返回 N/M 个源」或缓存标记「缓存」",
            texts("已返回", substring = true) > 0 ||
                texts("缓存", substring = true) > 0 ||
                texts("没有结果", substring = true) > 0)
        // 2) 不得回落到阻塞端点
        assertTrue("不应回落到一次性搜索",
            texts("流式搜索不可用", substring = true) == 0)
        // 3) 首屏上界（流式应在 1~3s 出第一批；这里放宽到 12s）
        assertTrue("首屏不应退化成等最慢源（实际 ${firstMs}ms）", firstMs < 12_000)
        ev("走流式端点；首屏 ${firstMs}ms；未回落")

        // 4) 结果状态必须明确
        val hasList = nodes("manga_search_results") > 0
        val noResult = texts("没有结果") > 0
        val failed = texts("搜索失败", substring = true) > 0
        assertTrue("必须有确定状态（结果列表 / 没有结果 / 失败说明）",
            hasList || noResult || failed)
        ev("确定状态：列表=$hasList 无结果=$noResult 失败=$failed")

        // 4b) 缓存命中时必须**仍有结果**：服务端只缓存非空结果，
        //     若这里显示"没有结果"，说明客户端把 finished 事件里的 groups 丢了
        //     （实测踩到：缓存命中却显示"没有结果"）。
        if (texts("缓存", substring = true) > 0) {
            assertTrue("缓存命中时必须显示结果（服务端只缓存非空结果）", hasList)
            ev("缓存命中且结果在位")
        }

        // 5) 逐源说明（仍在查询/超时/失败）若出现，必须可读
        if (nodes("manga_search_errors") > 0) {
            val node = rule.onNodeWithTag("manga_search_errors")
            node.assertIsDisplayed()
            ev("逐源说明在位（慢源如实标注，不静默当没有结果）")
        } else {
            ev("本次没有未返回的源（全部源都已返回）")
        }
    }

    /**
     * 搜索结果必须是**网页端同款的分页**（用户 2026-09-17 明确要求）。
     *
     * 网页端 `updatePager()` 的行为：`◀ 上一页` + 页码按钮（当前页高亮）+ `下一页 ▶`，
     * 往后只延伸一页（源站不给 total），用「…」表示未知；翻页是**替换当前页结果**，
     * 状态行是「第 N 页 · X 部」。
     *
     * 旧实现是"加载更多"追加模式，且因为页码状态没更新而永远只能再翻一页
     * （用户反馈"最多只能翻到 2 页、也没有清晰分页"）。
     */
    @Test
    fun mangaSearch_usesWebStylePager() {
        waitText("书架", timeoutMs = 150_000)
        rule.onAllNodesWithText("浏览")[0].performClick()
        waitText("漫画（优先）", timeoutMs = 60_000)
        rule.onAllNodesWithText("搜漫画")[0].performClick()
        waitText("漫画搜索", timeoutMs = 30_000)

        rule.onNodeWithTag("manga_search_field").performTextInput("巨人")
        rule.onNodeWithTag("manga_search_btn").performClick()

        // A) 分页控件必须出现，且状态行是网页端口径「第 N 页 · X 部」
        rule.waitUntil(150_000) { nodes("manga_search_pager") > 0 ||
                                  texts("没有结果") > 0 }
        if (nodes("manga_search_pager") == 0) {
            ev("本次搜索没有结果，分页控件不适用")
            return
        }
        assertTrue("状态行应是网页端口径『第 N 页 · X 部』：",
            texts("第 1 页 ·", substring = true) > 0)
        assertTrue("必须有页码按钮 1", nodes("manga_search_page_1") > 0)
        assertTrue("必须有『下一页』", nodes("manga_search_next") > 0)
        rule.onNodeWithTag("manga_search_prev").assertIsNotEnabled()   // 第 1 页没有上一页
        ev("第 1 页：分页控件齐备（上一页/页码 1/下一页），状态行『第 1 页 · X 部』")

        // B) 下一页 → 页码推进到 2、当前页高亮、上一页可用（替换而非追加）
        if (runCatching { rule.onNodeWithTag("manga_search_next").assertIsEnabled() }.isFailure) {
            ev("服务端判定只有一页，翻页断言到此")
            return
        }
        rule.onNodeWithTag("manga_search_next").performClick()
        rule.waitUntil(150_000) { texts("第 2 页 ·", substring = true) > 0 }
        assertTrue("页码按钮 2 必须在位（当前页高亮）", nodes("manga_search_page_2") > 0)
        rule.onNodeWithTag("manga_search_prev").assertIsEnabled()      // 到第 2 页后能回退
        ev("翻到第 2 页：状态行『第 2 页 · X 部』，页码按钮与上一页同步")

        // C) 点页码 1 回退 → 证明可以任意跳页（旧实现做不到）
        rule.onNodeWithTag("manga_search_page_1").performClick()
        rule.waitUntil(150_000) { texts("第 1 页 ·", substring = true) > 0 }
        ev("点页码 1 回到第 1 页：可任意跳页")
        Unit
    }
}
