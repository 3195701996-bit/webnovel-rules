package com.webnovel.mobile

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToIndex
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeLeft
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * 阅读器**页加载失败可就地重试**（方向基线 §8.C："长图、图片失败重试…"）。
 *
 * 此前失败页只显示"第 N 页加载失败（可稍后重试）"，**没有任何重试入口**——
 * 用户只能退出章节再进来（丢掉滚动位置）。本用例用自检漫画夹具验证：
 *   1. 把第 1 话第 2 页的文件写成坏字节（能列出页、但解码必失败）→ 该页显示失败；
 *   2. 页上必须有「重试这一页」按钮；
 *   3. 把文件恢复成正常图片后点重试 → 失败提示消失、该页正常显示。
 *
 * 夹具不依赖网络，因此这条链路是确定性的（不受源站抖动影响）。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class ReaderPageRetryTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private lateinit var comic: SelfTestComic

    private fun ev(line: String) = println("PAGE_RETRY_EVIDENCE $line")

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext

    private fun texts(t: String, substring: Boolean = false) =
        rule.onAllNodesWithText(t, substring = substring).fetchSemanticsNodes().size

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size

    private fun waitText(t: String, timeoutMs: Long = 120_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) { texts(t, substring) > 0 }
    }

    @Before
    fun setUp() {
        // 用**每次运行都不同**的 comic id：Coil 的图片缓存是按 URL 键的，
        // 复用同一个 id 时会拿到上一轮成功解码的缓存图，"坏页"根本不失败
        // （实测踩到：坏字节写进去了，界面照旧显示老图，失败提示不出现）。
        val uniq = "__selftest_retry_%d__".format(System.currentTimeMillis() % 1_000_000)
        comic = SelfTestComic(comicId = uniq)
        comic.create()
    }

    @After
    fun tearDown() {
        comic.cleanup()
        assertTrue("自检漫画未清理干净", !comic.exists())
    }

    @Test
    fun failedPageCanBeRetriedInPlace() {
        // 1) 先把第 1 话第 2 页写成坏字节：页数照旧（按文件名列出），但解码必失败
        val page2 = File(OfflineStore.runtimeDir(ctx),
            "manga/downloads/${comic.sourceKey}/${comic.comicIdValue}/${SelfTestComic.CH1_ID}/0001.jpg")
        assertTrue("夹具的第 2 页应存在：$page2", page2.isFile)
        val good = page2.readBytes()
        page2.writeBytes(ByteArray(4096) { 0x41 })          // 全是 'A'，不是图片
        ev("已把第 2 页写成坏字节（${page2.length()} 字节）")

        // 2) 进书架 → 自检漫画 → 开始阅读
        waitText(SelfTestComic.TITLE, timeoutMs = 150_000)
        rule.onAllNodesWithText(SelfTestComic.TITLE)[0].performClick()
        waitText("开始阅读", timeoutMs = 60_000)
        rule.onAllNodesWithText("开始阅读")[0].performClick()
        waitText("1 / 3", timeoutMs = 60_000, substring = true)
        ev("进入原生阅读器")

        // 3) 第 2 页必须显示失败，并且**有重试入口**。
        //    若当前是"横向翻页"模式，得先翻到第 2 页——离屏页不会被组合出来，
        //    等它的失败文案永远等不到（实测踩到：阅读偏好是跨用例持久化的）。
        if (nodes("manga_pager") > 0) {
            rule.onNodeWithTag("manga_pager").performTouchInput { swipeLeft() }
            ev("横向翻页模式：已翻到第 2 页")
        } else {
            // 纵向连续模式：第 1 页（240×600 的图按宽度铺满）就占满整屏，
            // 第 2 页在屏幕外 → LazyColumn 根本不会组合它，失败文案自然等不到。
            // 必须滚到第 2 项（实测踩到：等 60s 也没有，误以为重试没生效）。
            rule.onNodeWithTag("manga_pages").performScrollToIndex(1)
            ev("纵向连续模式：已滚动到第 2 页")
        }
        rule.waitUntil(60_000) { texts("第 2 页加载失败", substring = true) > 0 }
        assertTrue("失败页必须有「重试这一页」按钮（此前没有任何重试入口）",
            nodes("manga_page_retry_1") > 0)
        ev("坏页如实显示失败，且出现「重试这一页」")

        // 4) 恢复成正常图片 → 点重试 → 失败提示必须消失
        page2.writeBytes(good)
        ev("已恢复第 2 页文件（${page2.length()} 字节）")
        rule.onNodeWithTag("manga_page_retry_1").performClick()
        rule.waitUntil(60_000) { texts("第 2 页加载失败", substring = true) == 0 }
        assertTrue("重试后失败提示必须消失（不靠退出章节再进）",
            texts("第 2 页加载失败", substring = true) == 0)
        // 该页重新出现在语义树里（内容描述"第 2 页"）
        rule.waitUntil(30_000) {
            rule.onAllNodes(androidx.compose.ui.test.hasContentDescription("第 2 页"))
                .fetchSemanticsNodes().isNotEmpty()
        }
        ev("重试后第 2 页正常显示（无需退出章节）")
    }
}
