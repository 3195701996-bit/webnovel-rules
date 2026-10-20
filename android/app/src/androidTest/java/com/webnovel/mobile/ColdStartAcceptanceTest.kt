package com.webnovel.mobile

import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.assertIsDisplayed
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 冷启动验收（阶段 C 诊断第七节）：**从点击 App 图标开始**，而不是只测服务路径。
 *
 * 断言：
 *   1. 冷启动不显示调试控制台（旧首页的"阶段 A"标题与三按钮必须消失）；
 *   2. 引擎未就绪时显示可读的启动状态，就绪后进入书架（有内容或空书架引导），
 *      而不是黑屏；
 *   3. 空书架显示漫画搜索引导；有书架数据时四个主导航页签都在位。
 */
@RunWith(AndroidJUnit4::class)
class ColdStartAcceptanceTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    @Test
    fun coldStart_showsShelfNotConsole() {
        // 1) 调试控制台必须已经移除（旧首页的"阶段 A"标题与三按钮）
        rule.waitForIdle()
        rule.onAllNodesWithText("阶段 A", substring = true).assertCountEquals(0)
        rule.onAllNodesWithText("服务未就绪", substring = true).assertCountEquals(0)

        // 2) 启动过程必须有可读状态（不能黑屏无内容）
        rule.waitUntil(timeoutMillis = 20_000) {
            rule.onAllNodesWithText("正在启动", substring = true).fetchSemanticsNodes().isNotEmpty() ||
            rule.onAllNodesWithText("正在校验", substring = true).fetchSemanticsNodes().isNotEmpty() ||
            rule.onAllNodesWithText("书架").fetchSemanticsNodes().isNotEmpty() ||
            rule.onAllNodesWithText("本机引擎未就绪").fetchSemanticsNodes().isNotEmpty()
        }

        // 3) 等引擎落定：有数据时显示三个书架分段；全新安装时显示明确的
        //    空书架引导。两者都是有效首页，不依赖旧版小说书架文案。
        rule.waitUntil(timeoutMillis = 300_000) {
            val shelf = rule.onAllNodesWithText("最近阅读").fetchSemanticsNodes().isNotEmpty() &&
                    rule.onAllNodesWithText("已缓存").fetchSemanticsNodes().isNotEmpty() &&
                    rule.onAllNodesWithText("收藏").fetchSemanticsNodes().isNotEmpty()
            val emptyShelf = rule.onAllNodesWithText("书架还是空的")
                .fetchSemanticsNodes().isNotEmpty()
            val failed = rule.onAllNodesWithText("本机引擎未就绪").fetchSemanticsNodes().isNotEmpty()
            shelf || emptyShelf || failed
        }

        // 4) 不允许以启动失败收场（这台设备上引擎应当可用）
        rule.onAllNodesWithText("本机引擎未就绪").assertCountEquals(0)

        // 5) 首用空态必须给出原生漫画搜索入口；有数据时再断言主导航页签。
        if (rule.onAllNodesWithText("书架还是空的").fetchSemanticsNodes().isNotEmpty()) {
            rule.onNodeWithText("搜漫画").assertIsDisplayed()
        } else {
            rule.onAllNodesWithText("书架")[0].assertIsDisplayed()
            rule.onNodeWithText("浏览").assertExists()
            rule.onNodeWithText("下载").assertExists()
            rule.onNodeWithText("设置").assertExists()
        }
    }
}
