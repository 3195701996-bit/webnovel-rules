package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.printToLog
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 漫画「分类浏览」界面验收（漫画优先）。
 *
 * 这是 MangaBrowsePathTest 的界面侧对照：路径测试证明接口能给数据，
 * 本用例证明**界面上点得到、看得见、点得进**：
 *   浏览页 → 漫画分类按钮 → 结果封面网格 → 点卡片进原生漫画详情。
 *
 * 网络是会变的：分类取数失败时**允许失败**，但必须显示可读原因并给出证据，
 * 不能假装成功（也不静默跳过）。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class MangaBrowseUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private fun ev(line: String) = println("MANGA_BROWSE_UI_EVIDENCE $line")

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size

    private fun texts(text: String, substring: Boolean = false) =
        rule.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().size

    private fun waitText(text: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) { texts(text, substring) > 0 }
    }

    @Test
    fun mangaCategory_cardsOpenMangaDetail() {
        // 1) 冷启动 → 浏览 页签 → 探索
        //    注：浏览页已按方向基线收敛为"漫画优先"，入口文案随之变化，
        //    这里跟随的是**界面文案**（测试假设），产品目标未变。
        waitText("书架", timeoutMs = 150_000)
        rule.onAllNodesWithText("浏览")[0].performClick()
        waitText("漫画（优先）", timeoutMs = 60_000)
        rule.onAllNodesWithText("探索（", substring = true)[0].performClick()

        // 2) 漫画分类区必须在位（源与分类都来自适配器自己声明）
        waitText("漫画分类（来自适配器自己声明的排名/分类）", timeoutMs = 150_000)
        ev("探索页已显示漫画分类区")
        rule.onNodeWithTag("explore_sources").performScrollToNode(
            hasTestTag("manga_cat_mangadex_popular"))
        rule.onNodeWithTag("manga_cat_mangadex_popular").assertIsDisplayed()
        ev("漫画分类按钮在位：MangaDex · 按热度")

        // 3) 点分类 → 必须切到**独立的结果视图**（不是把结果追加在 19 个按钮下方）
        rule.onNodeWithTag("manga_cat_mangadex_popular").performClick()
        waitText("MangaDex · 按热度", timeoutMs = 30_000)
        ev("已进入分类结果视图（标题=源 · 分类）")

        // 4) 等结果：成功=出现卡片；失败=必须出现可读原因（错误或"没有取到内容"）
        rule.waitUntil(120_000) {
            nodes("manga_browse_card") > 0 ||
                texts("读取分类失败", substring = true) > 0 ||
                texts("本次没有取到内容", substring = true) > 0
        }
        if (nodes("manga_browse_card") == 0) {
            val why = texts("读取分类失败", substring = true) > 0 ||
                texts("本次没有取到内容", substring = true) > 0
            ev("分类取数未成功（如实显示原因，不假装有数据）：原因在位=$why")
            assertTrueMsg("取数失败时必须显示可读原因", why)
            return
        }
        val cards = nodes("manga_browse_card")
        assertTrueMsg("结果视图里必须至少有一张卡片", cards > 0)
        ev("分类结果卡片数=$cards（封面网格）")
        // 「加载更多」在结果末尾（首屏之外）：滚动到它再断言，证明翻页入口真的可达。
        // 同时**故意把列表停在底部**再进详情——返回后如果滚动位置被重置，
        // 下一步的"无需滚动即可见"断言就会失败（这是滚动恢复的真实检验）。
        rule.onNodeWithTag("manga_browse_list").performScrollToNode(hasText("加载更多"))
        rule.onNodeWithText("加载更多").assertIsDisplayed()
        ev("结果视图含『加载更多』入口（滚动可达）")

        // 5) 点当前可见的最后一张卡片 → 进原生漫画详情
        val cardNodes = rule.onAllNodesWithTag("manga_browse_card")
        val lastVisible = cardNodes.fetchSemanticsNodes().size - 1
        assertTrueMsg("应有可见卡片可点（实际 $lastVisible+1 张）", lastVisible >= 0)
        cardNodes[lastVisible].performClick()
        // 详情页话数是动态的（共 N 话 · 已下载 M 话）→ 必须按子串匹配
        waitText("话 · 已下载", timeoutMs = 120_000, substring = true)
        rule.onNodeWithText("话 · 已下载", substring = true).assertIsDisplayed()
        ev("点卡片已进入漫画详情（显示『共 N 话 · 已下载 M 话』）")

        // 6) 导航可逆**且状态保留**（方向基线 §5.2 必达要求）：
        //    返回后必须仍在原来的分类结果里，并且**滚动位置也回到离开时的位置**。
        rule.onAllNodesWithText("← 返回")[0].performClick()
        // 注意：caption/list 是 **testTag**，要用 onAllNodesWithTag 判断
        // （上一版误用 onAllNodesWithText 找 tag 名，明明恢复了却等到超时）
        try {
            rule.waitUntil(30_000) {
                nodes("manga_browse_caption") > 0 || nodes("manga_cat_mangadex_popular") > 0
            }
        } catch (t: Throwable) {
            rule.onRoot().printToLog("MBTREE")
            ev("返回后未匹配到分类结果视图，已输出语义树（见 MBTREE）")
            throw t
        }
        assertTrueMsg(
            "返回后应仍在分类结果视图（caption=${nodes("manga_browse_caption")} " +
                "源分类按钮=${nodes("manga_cat_mangadex_popular")}）",
            nodes("manga_browse_caption") > 0)
        rule.onNodeWithTag("manga_browse_list").assertIsDisplayed()
        val afterBack = rule.onAllNodesWithTag("manga_browse_card")
            .fetchSemanticsNodes().size
        assertTrueMsg("返回后结果卡片应仍在（返回后 $afterBack 张）", afterBack > 0)
        // 关键：不做任何滚动，直接断言列表底部的"加载更多"仍可见 = 滚动位置被恢复
        rule.onNodeWithText("加载更多").assertIsDisplayed()
        ev("返回后仍在分类结果里：卡片 $afterBack 张，且『加载更多』无需滚动即可见（滚动位置已恢复）")

        // 7) 再返回一层才回到探索页（层级没有被状态恢复打乱）
        rule.onAllNodesWithText("← 返回")[0].performClick()
        waitText("漫画分类（来自适配器自己声明的排名/分类）", timeoutMs = 60_000)
        ev("再返回一次回到探索页（层级正确）")
    }

    private fun assertTrueMsg(msg: String, cond: Boolean) {
        org.junit.Assert.assertTrue(msg, cond)
    }
}
