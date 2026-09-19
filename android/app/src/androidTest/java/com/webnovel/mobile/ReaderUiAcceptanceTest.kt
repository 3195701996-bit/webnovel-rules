package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.hasText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 原生阅读界面的验收（真机/模拟器上从**应用图标冷启动**开始，不 bindService 走捷径）：
 *
 *   书架（含继续阅读） → 详情（目录/已下载/失败状态） → 原生阅读器（正文渲染）
 *   → 未下载章节的诚实分支（不偷偷联网、不假装有正文） → 目录 → 阅读设置
 *   → 返回详情，按钮由"开始阅读"变为"继续阅读 第 1 章"（证明进度真的写进了引擎）
 *
 * 内容来自合成书（SelfTestBook），因此不依赖外部书源与网络，可重复执行。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class ReaderUiAcceptanceTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private lateinit var book: SelfTestBook

    private fun ev(line: String) = println("READER_UI_EVIDENCE $line")

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
        book = SelfTestBook()
        book.create()
        ev("已造自检书 key=${book.bookKey}")
    }

    @After
    fun tearDown() {
        book.cleanup()
        assertFalse("自检书目录未清理干净", book.exists())
        assertEquals("阅读进度里有自检残留", 0, book.progressEntryCount())
        ev("清理完成 残留=${book.progressEntryCount()}")
    }

    @Test
    fun shelfToDetailToNativeReader() {
        // 1) 冷启动落到书架，并列出合成书
        waitText(SelfTestBook.BOOK_NAME)
        ev("书架已列出 ${SelfTestBook.BOOK_NAME}")
        rule.onAllNodesWithText(SelfTestBook.BOOK_NAME)[0].assertIsDisplayed()

        // 2) 进入详情：目录统计与失败章节照实标注
        clickText(SelfTestBook.BOOK_NAME)
        waitText("开始阅读")
        rule.onNodeWithText("目录 3 章 · 已下载 1 章 · 失败 1 章").assertIsDisplayed()
        // 作者与章数是同一行合并语义节点，按整行断言
        rule.onNodeWithText("自检 · 共 3 章").assertIsDisplayed()
        ev("详情页：目录统计、失败标注与作者行均可见")

        // 2b) 详情页应提供"从书架删除"入口（点了会弹确认框，本用例不点确认）
        rule.onNodeWithText("从书架删除").assertIsDisplayed()
        ev("详情页含『从书架删除』入口")

        // 3) 进入原生阅读器：渲染第一段正文
        clickText("开始阅读")
        waitText(SelfTestBook.CHAPTER1_BODY_1, substring = true)
        rule.onNodeWithText(SelfTestBook.CHAPTER1_BODY_2, substring = true).assertIsDisplayed()
        // 章节名出现在标题栏与正文标题两处（正文里的重复首行已去重），故取第一个节点
        rule.onAllNodesWithText(SelfTestBook.CHAPTER1_NAME, substring = true)[0].assertIsDisplayed()
        ev("阅读器：第 1 章正文两段均渲染")

        // 4) 阅读器骨架在位（原生控件，不是网页）
        rule.onNodeWithText("上一章").assertIsDisplayed()
        // "下一章"在底部栏与正文末尾各有一个，取第一个即可
        rule.onAllNodesWithText("下一章")[0].assertIsDisplayed()
        rule.onNodeWithText("目录").assertIsDisplayed()
        rule.onNodeWithText("Aa").assertIsDisplayed()
        rule.onNodeWithText("1 / 3", substring = true).assertIsDisplayed()

        // 5) 未下载章节：明确提示 + 手动获取入口，不自动联网、不显示空正文
        clickText("下一章")
        waitText("第 2 章尚未下载")
        rule.onNodeWithText("在线获取本章").assertIsDisplayed()
        rule.onNodeWithText(SelfTestBook.CHAPTER1_BODY_1, substring = true).assertDoesNotExist()
        ev("第 2 章：显示未下载分支与在线获取入口")
        rule.onNodeWithText("上一章", substring = false).performClick()   // 回到第 1 章
        waitText(SelfTestBook.CHAPTER1_BODY_1, substring = true)

        // 6) 目录覆盖层：列出全部章节并标注状态
        clickText("目录")
        waitText("目录（3 章）")
        // 阅读器正文列表仍在覆盖层之下，故按 testTag 指定目录列表
        rule.onNodeWithTag("toc_list")
            .performScrollToNode(hasText(SelfTestBook.CHAPTER3_NAME, substring = true))
        rule.onNodeWithText(SelfTestBook.CHAPTER3_NAME, substring = true).assertIsDisplayed()
        ev("目录覆盖层：含第 3 章")
        clickText("关闭")

        // 7) 阅读设置：字号/行距/主题，且立即生效不崩溃
        clickText("Aa")
        waitText("阅读设置")
        rule.onNodeWithText("字号", substring = true).assertIsDisplayed()
        rule.onNodeWithText("行距", substring = true).assertIsDisplayed()
        clickText("夜间")
        rule.onNodeWithText("阅读设置").assertIsDisplayed()   // 切主题后仍在设置页
        ev("阅读设置：字号/行距/主题可见，切换到夜间正常")
        clickText("关闭")
        waitText(SelfTestBook.CHAPTER1_BODY_1, substring = true)

        // 8) 返回详情：进度已写入引擎 → 按钮变为"继续阅读 第 1 章"
        clickText("← 返回")
        waitText("继续阅读 第 1 章", timeoutMs = 20_000)
        ev("返回详情：按钮已变为『继续阅读 第 1 章』（进度已落库）")

        // 9) 返回书架：该书出现在"继续阅读"，副标题带进度
        clickText("← 返回")
        waitText(SelfTestBook.BOOK_NAME)
        // 该书同时出现在"继续阅读"与"小说"分区，故取第一个匹配
        rule.onAllNodesWithText("读到 ${SelfTestBook.CHAPTER1_NAME}", substring = true)[0]
            .assertIsDisplayed()
        ev("书架：显示读到 ${SelfTestBook.CHAPTER1_NAME}")
    }
}
