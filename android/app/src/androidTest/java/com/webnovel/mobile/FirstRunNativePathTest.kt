package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 首用路径验收：空书架 → 原生漫画，**不得出现桌面网页**（方向基线 P0-B / §8.A）。
 *
 * 这条用例对应的是用户实际看到的那个界面：原生外壳里嵌着桌面网页，
 * 标题「浏览与搜索」，页面上还挂着"当前页面地址已失效"的回环误报。
 * 复现方式就是冷启动后点空书架上的按钮——过去那个按钮直接 `Dest.Web("/")`。
 *
 * 断言口径（不看"页签存在"这种弱证据）：
 *   1. 空书架上的入口**必须**是原生目的地：点了之后出现原生搜索控件；
 *   2. 整条路径上**不得出现桌面网页特征**（桌面首页按钮、局域网地址提示）；
 *   3. 浏览页不再并列"网页搜索页"入口；网页只在设置里，且标注为诊断页。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class FirstRunNativePathTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private fun ev(line: String) = println("FIRST_RUN_EVIDENCE $line")

    private fun texts(text: String, substring: Boolean = false) =
        rule.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().size

    private fun waitText(text: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) { texts(text, substring) > 0 }
    }

    /** 桌面网页独有的特征串：出现在屏幕上就说明又套壳了 */
    private val webMarkers = listOf("进入我的书库", "当前 IP 地址", "固定地址（换网不掉线）")

    private fun webMarkerHits(): Int = webMarkers.sumOf { texts(it, substring = true) }

    @Test
    fun firstRun_goesNative_notWebView() {
        // 1) 冷启动到书架
        waitText("书架", timeoutMs = 150_000)
        val emptyShelf = texts("书架还是空的") > 0
        ev("冷启动完成；空书架=$emptyShelf")

        if (emptyShelf) {
            // 2) 空书架的入口必须原生（漫画优先），且不再有单个"去搜索"跳网页
            rule.onNodeWithTag("empty_shelf_search_manga").assertIsDisplayed()
            rule.onNodeWithTag("empty_shelf_browse_manga").assertIsDisplayed()
            rule.onNodeWithTag("empty_shelf_search_novel").assertIsDisplayed()
            ev("空书架入口：搜漫画 / 浏览内置漫画源 / 搜小说（全原生）")

            // 3) 点「搜漫画」→ 必须落到原生搜索控件
            rule.onNodeWithTag("empty_shelf_search_manga").performClick()
            waitText("漫画搜索", timeoutMs = 30_000)
            rule.onNodeWithTag("manga_search_field").assertIsDisplayed()
            assertEquals("首用路径上不能出现桌面网页特征", 0, webMarkerHits())
            Views.assertNoWebView(rule.activity, "空书架→原生漫画搜索")
            ev("点「搜漫画」→ 原生漫画搜索（无桌面网页特征）")
            rule.onAllNodesWithText("← 返回")[0].performClick()
            waitText("书架还是空的", timeoutMs = 30_000)

            // 4) 点「浏览内置漫画源」→ 必须落到原生源清单，并能进单源页
            rule.onNodeWithTag("empty_shelf_browse_manga").performClick()
            waitText("内置漫画源", timeoutMs = 30_000)
            rule.onNodeWithTag("manga_source_list").assertIsDisplayed()
            assertEquals("浏览内置源路径上不能出现桌面网页", 0, webMarkerHits())
            Views.assertNoWebView(rule.activity, "浏览内置漫画源")
            ev("点「浏览内置漫画源」→ 原生源清单（无桌面网页特征）")
        } else {
            // 书架非空（设备上有书）时无法走空态分支：如实记录，不假装验证过
            ev("书架非空，空态分支本次不可用（需要在干净书库的设备上验收）")
        }

        // 5) 浏览页：核心入口全原生，网页入口不再并列。
        //    注意：压栈页面（搜索/源清单）里页签栏不可见，必须回到主框架。
        //    过去这里**无条件**点"← 返回"并等"书架还是空的"——书架非空时必然超时，
        //    是评审指出的脆弱前置条件（2026-09-15）。现在改成：有返回就点，
        //    回到主框架即可（空库/非空库都成立）。
        while (texts("← 返回") > 0) {
            rule.onAllNodesWithText("← 返回")[0].performClick()
            rule.waitForIdle()
        }
        waitText("浏览", timeoutMs = 30_000)
        rule.onAllNodesWithText("浏览")[0].performClick()
        waitText("漫画（优先）", timeoutMs = 60_000)
        rule.onNodeWithText("内置漫画源", substring = true).assertIsDisplayed()
        assertEquals("浏览页不应再有'网页搜索页'入口", 0, texts("网页搜索页", substring = true))
        assertEquals("浏览页不应显示桌面网页", 0, webMarkerHits())
        Views.assertNoWebView(rule.activity, "浏览页")
        ev("浏览页：漫画优先，无网页搜索入口")

        // 6) 漫画源卡片必须是**真实入口**（过去是空回调，点了没反应）
        val cards = rule.onAllNodesWithText("依赖：", substring = true).fetchSemanticsNodes().size
        assertTrue("漫画源卡片应在位（实际 $cards）", cards > 0)
        rule.onAllNodesWithText("依赖：", substring = true)[0].performClick()
        waitText("实测结论", timeoutMs = 30_000, substring = true)
        rule.onNodeWithTag("manga_source_page").assertIsDisplayed()
        rule.onNodeWithText("可浏览的分类", substring = true).assertIsDisplayed()
        Views.assertNoWebView(rule.activity, "原生漫画源页")
        ev("漫画源卡片 → 原生源页（能力/实测结论/分类）")

        // 7) 网页只在设置里，且明确标注为诊断页（这一页是唯一允许出现 WebView 的）
        rule.onAllNodesWithText("← 返回")[0].performClick()
        waitText("漫画（优先）", timeoutMs = 30_000)
        rule.onAllNodesWithText("设置")[0].performClick()
        waitText("高级（网页诊断）", timeoutMs = 60_000)
        // 设置页是**可滚动**的长页：加了「存储管理」入口后内容变长，底部说明会落到
        // 折叠线下。断言"显示中"之前必须先滚到它（实测踩到：assertIsDisplayed 失败，
        // 而节点其实存在、也不是网页）。
        rule.onNodeWithTag("build_info").performScrollTo().assertIsDisplayed()
        rule.onNodeWithText("不是日常使用界面", substring = true)
            .performScrollTo().assertIsDisplayed()
        ev("设置：网页入口收进「高级（网页诊断）」，并显示产物基线")
    }
}
