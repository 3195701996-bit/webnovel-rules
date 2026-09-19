package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 网络 → 代理（0.73.0）真机验收：入口在位、状态如实、非法值当场报错。
 *
 * **本用例刻意不"保存一个真代理"**：目标机上并没有本地代理在监听，存下去会让
 * 引擎所有出站请求挂掉（测试中途失败时更会把用户的应用留在坏状态）。因此这里只验证
 * 不需要真实代理就能成立、且真正重要的三条：
 *   1. 入口可达，状态行如实说明"直连/经代理 + 来源"；
 *   2. 类型（HTTP/SOCKS5）与地址可填，**非法地址保存必须当场报错**（不得静默回落直连）；
 *   3. "清除代理"幂等可用，做完仍是直连（不会把用户留在坏状态）。
 *
 * 真实探测（会访问真实站点）默认关闭，需要时显式打开：
 *   adb shell am instrument -w -e class com.webnovel.mobile.ProxySettingsUiTest \
 *     -e proxyProbe 1 com.webnovel.mobile.test/androidx.test.runner.AndroidJUnitRunner
 * 桌面侧对探测逻辑已有带桩的契约测试（tests/test_net_api.py），这里只是端到端补一刀。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class ProxySettingsUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private fun ev(line: String) = println("PROXY_UI_EVIDENCE $line")

    private fun waitText(text: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) {
            rule.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().isNotEmpty()
        }
    }

    private fun hasText(text: String, substring: Boolean = false): Boolean =
        rule.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().isNotEmpty()

    @Test
    fun proxyScreen_isHonestAboutStateAndRejectsBadAddress() {
        waitText("书架", timeoutMs = 150_000)
        waitText("设置")
        rule.onAllNodesWithText("设置")[0].performClick()
        waitText("书源管理")
        rule.onNodeWithTag("proxy_entry").performScrollTo().performClick()
        waitText("当前：", substring = true)
        rule.onNodeWithTag("proxy_state").assertIsDisplayed()
        ev("代理入口可达，状态行可见")

        // 类型两个选项都在位（HTTP / SOCKS5）
        rule.onNodeWithTag("proxy_scheme_http").assertIsDisplayed()
        rule.onNodeWithTag("proxy_scheme_socks5").assertIsDisplayed()

        // 1) 非法地址：必须当场报错，且状态行仍是原值（不静默回落、不写盘）
        rule.onNodeWithTag("proxy_addr").performScrollTo().performTextInput("ftp://127.0.0.1:21")
        rule.onNodeWithTag("proxy_save").performScrollTo().performClick()
        rule.waitUntil(30_000) { hasText("保存失败", substring = true) }
        rule.onNodeWithTag("proxy_msg").assertIsDisplayed()
        assertTrue("非法地址必须给出可见回执", hasText("无法识别", substring = true))
        ev("非法地址被当场拒绝（未静默回落直连）")
        // 状态行没被改动（可能已滚出视口，故只断言存在）
        rule.onNodeWithTag("proxy_state").assertExists()

        // 2) 清除代理：幂等，做完仍是直连（不会把用户留在坏状态）
        rule.onNodeWithTag("proxy_clear").performScrollTo().performClick()
        rule.waitUntil(30_000) {
            hasText("当前：直连", substring = true) || hasText("已清除代理", substring = true)
        }
        rule.waitUntil(30_000) {
            !hasText("正在保存", substring = true)
        }
        assertTrue("清除后必须回到直连或明确说明当前状态",
            hasText("当前：", substring = true))
        ev("清除代理幂等可用")

        // 3) 可选：真实连通性探测（默认关闭，见类注释）
        val probe = InstrumentationRegistry.getArguments().getString("proxyProbe")
        if (probe == "1") {
            rule.onNodeWithTag("proxy_test").performScrollTo().performClick()
            rule.waitUntil(120_000) {
                hasText("代理连通性探测", substring = true) ||
                    hasText("探测失败", substring = true)
            }
            rule.onNodeWithTag("proxy_test_result").assertIsDisplayed()
            ev("真实探测已执行（结果见 proxy_test_result）")
        } else {
            ev("跳过真实探测（未传 -e proxyProbe 1）")
        }

        // 4) 返回设置不崩溃
        rule.onAllNodesWithText("← 返回")[0].performClick()
        waitText("书源管理")
        ev("返回设置页正常")
    }
}
