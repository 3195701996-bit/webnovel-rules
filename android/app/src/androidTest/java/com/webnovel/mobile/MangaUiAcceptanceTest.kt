package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.click
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.getUnclippedBoundsInRoot
import androidx.compose.ui.test.onAllNodesWithContentDescription
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeLeft
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.assertFalse
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 原生漫画界面的验收（从应用图标冷启动，不 bindService 走捷径）：
 *
 *   书架（漫画网格） → 详情（话数/已下载/未下载照实标注） → 原生阅读器（纵向连续看图）
 *   → 目录 → 下一话 → 返回详情（按钮变"继续阅读 第 2 话 承"） → 返回书架（读到 第 2 话 承 P1）
 *
 * 全部命中本地已下载图片，不请求源站；合成漫画在结束时清理干净。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class MangaUiAcceptanceTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private lateinit var comic: SelfTestComic

    private fun ev(line: String) = println("MANGA_UI_EVIDENCE $line")

    private fun waitText(text: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) {
            rule.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().isNotEmpty()
        }
    }

    private fun clickText(text: String) {
        waitText(text)
        rule.onAllNodesWithText(text)[0].performClick()
    }

    @Before
    fun setUp() {
        comic = SelfTestComic()
        comic.create()
        ev("已造自检漫画 ${comic.sourceKey}/${comic.comicIdValue}")
    }

    @After
    fun tearDown() {
        comic.cleanup()
        assertFalse("自检漫画目录未清理干净", comic.exists())
        assertEquals("书库里有自检残留", 0, comic.libraryEntryCount())
        assertEquals("历史里有自检残留", 0, comic.historyEntryCount())
        ev("清理完成 书库残留=${comic.libraryEntryCount()} 历史残留=${comic.historyEntryCount()}")
    }

    @Test
    fun shelfToMangaDetailToNativeReader() {
        // 1) 冷启动书架：漫画网格列出合成漫画
        waitText(SelfTestComic.TITLE)
        ev("书架已列出 ${SelfTestComic.TITLE}")
        rule.onAllNodesWithText(SelfTestComic.TITLE)[0].assertIsDisplayed()

        // 1b) 诚实性：没有"检查书库更新"结果时，书架不得编造"有新话"标记
        //     （自检漫画刚造出来，批次检查结果里没有它）
        val badges = rule.onAllNodesWithText("有新话", substring = true).fetchSemanticsNodes().size
        assertEquals("没有检查结果时不应出现有新话标记", 0, badges)
        ev("书架：无检查结果时不显示有新话标记（不编造更新）")

        // 2) 详情：话数与已下载/未下载照实标注
        clickText(SelfTestComic.TITLE)
        waitText("开始阅读")
        rule.onNodeWithText("共 3 话 · 已下载 2 话").assertIsDisplayed()
        ev("详情：共 3 话 · 已下载 2 话")
        rule.onNodeWithText(SelfTestComic.CH3_NAME, substring = true).assertIsDisplayed()
        rule.onNodeWithText("未下载").assertIsDisplayed()      // 第 3 话未下载（本地确实没有图）
        // 卷/话组标题：服务端返回 group 时必须显示出来（长连载靠它导航）
        rule.onNodeWithText(SelfTestComic.GROUP_A).assertExists()
        rule.onNodeWithText(SelfTestComic.GROUP_B).assertExists()
        ev("详情：显示卷分组「${SelfTestComic.GROUP_A}」「${SelfTestComic.GROUP_B}」")

        // 2b) 详情页应提供"从书库移除"入口（本用例只断言入口在位）
        rule.onNodeWithText("从书库移除").assertIsDisplayed()
        ev("详情页含『从书库移除』入口")

        // 2c) 按话勾选下载：只允许勾选未下载的话，选中后出现"下载选中（N 话）"
        //     （自检漫画第 3 话未下载，第 1/2 话已下载 → 只有第 3 话可勾）
        rule.onNodeWithTag("pick_${SelfTestComic.CH3_ID}").performClick()
        rule.waitUntil(20_000) {
            rule.onAllNodesWithText("下载选中（1 话）", substring = true)
                .fetchSemanticsNodes().isNotEmpty()
        }
        rule.onNodeWithText("清空选择").performClick()
        rule.waitUntil(20_000) {
            rule.onAllNodesWithText("下载选中", substring = true).fetchSemanticsNodes().isEmpty()
        }
        ev("按话勾选下载：未下载话可勾选，出现下载入口，可清空")

        // 3) 原生阅读器：纵向连续看图，页码来自真实页数
        clickText("开始阅读")
        waitText("1 / 3 · 1/2页")
        rule.onNodeWithTag("manga_pages").assertExists()
        // 图片加载失败时会显示占位文案；不应出现
        rule.onAllNodesWithText("页加载失败", substring = true).fetchSemanticsNodes().let {
            assertEquals("第 1 话出现图片加载失败占位", 0, it.size)
        }
        ev("阅读器：第 1 话 2 页已就绪，无失败占位")

        // 3b) 页面按图片原始比例排版：自检图是 240×600（高/宽=2.5）。
        //     曾经写死 aspectRatio(0.7)（高/宽≈1.43）会把长图裁掉，这里用实测比例锁住。
        rule.waitUntil(20_000) {
            rule.onAllNodesWithContentDescription("第 1 页").fetchSemanticsNodes().isNotEmpty()
        }
        val b = rule.onNodeWithContentDescription("第 1 页").getUnclippedBoundsInRoot()
        val ratio = (b.bottom - b.top).value / (b.right - b.left).value
        assertTrue("第 1 页渲染比例应接近原图 2.5，实际=${ratio}（写死宽高比会导致≈1.43）",
            ratio > 2.2 && ratio < 2.8)
        ev("第 1 页渲染比例=${"%.2f".format(ratio)}（原图 2.50）")

        // 3c) 缩放：点"放大"进入 2×，底栏显示倍数；再点"复位"回到 1×
        clickText("放大")
        waitText("缩放 2.0×", substring = true)
        rule.onNodeWithText("复位").assertIsDisplayed()
        ev("缩放进入 2.0×，出现复位入口")
        clickText("复位")
        rule.waitUntil(20_000) {
            rule.onAllNodesWithText("缩放 2.0×", substring = true).fetchSemanticsNodes().isEmpty()
        }
        waitText("放大")
        ev("复位后回到 1×（放大入口重新出现）")

        // 3d) 横向翻页模式：切到"翻页"后整页显示，左滑翻到第 2 页（进度随之更新）
        clickText("翻页")
        waitText("连续", substring = true)
        rule.waitUntil(20_000) {
            rule.onAllNodesWithTag("manga_pager").fetchSemanticsNodes().isNotEmpty()
        }
        rule.onNodeWithTag("manga_pager").performTouchInput { swipeLeft() }
        waitText("1 / 3 · 2/2页", timeoutMs = 20_000)
        ev("横向翻页：左滑后页码变为 2/2")
        // 点按翻页：左 1/3 上一页（单手阅读不必精确滑动）
        rule.onNodeWithTag("manga_pager").performTouchInput {
            click(androidx.compose.ui.geometry.Offset(width * 0.15f, height * 0.5f))
        }
        waitText("1 / 3 · 1/2页", timeoutMs = 20_000)
        ev("点按翻页：点左侧回到第 1 页")
        rule.onNodeWithTag("manga_pager").performTouchInput {
            click(androidx.compose.ui.geometry.Offset(width * 0.85f, height * 0.5f))
        }
        waitText("1 / 3 · 2/2页", timeoutMs = 20_000)
        ev("点按翻页：点右侧前进到第 2 页")
        clickText("连续")
        rule.waitUntil(20_000) {
            rule.onAllNodesWithTag("manga_pages").fetchSemanticsNodes().isNotEmpty()
        }
        ev("切回纵向连续模式")

        // 4) 目录：列出 3 话并标注已下载
        clickText("目录")
        waitText("目录（3 话）")
        rule.onNodeWithTag("manga_toc")
            .performScrollToNode(hasText(SelfTestComic.CH3_NAME, substring = true))
        rule.onNodeWithText(SelfTestComic.CH3_NAME, substring = true).assertIsDisplayed()
        ev("目录：含第 3 话")
        clickText("关闭")
        waitText("1 / 3 · 1/2页")

        // 5) 下一话：切到第 2 话（本地 1 页）
        clickText("下一话")
        waitText("2 / 3 · 1/1页")
        ev("切到第 2 话：1/1 页")

        // 6) 返回详情：进度已写入 → 按钮跟随历史
        clickText("← 返回")
        waitText("继续阅读 ${SelfTestComic.CH2_NAME}", timeoutMs = 20_000)
        ev("返回详情：按钮已变为『继续阅读 ${SelfTestComic.CH2_NAME}』")

        // 7) 返回书架：续读位置与页码来自同一份历史
        clickText("← 返回")
        waitText(SelfTestComic.TITLE)
        rule.onAllNodesWithText("读到 ${SelfTestComic.CH2_NAME} P1", substring = true)[0]
            .assertIsDisplayed()
        ev("书架：读到 ${SelfTestComic.CH2_NAME} P1")
    }
}
