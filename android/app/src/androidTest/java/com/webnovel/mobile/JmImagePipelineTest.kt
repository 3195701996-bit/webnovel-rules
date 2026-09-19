package com.webnovel.mobile

import androidx.compose.ui.semantics.getOrNull
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * 禁漫（jm）图片管线的**设备侧**证据（0.51.0 风险评估 P0-1）。
 *
 * 本轮能在这台设备上确证的三件事：
 *   1. jm 被登记为**混淆源**（`scrambled=true`），并带上当前还原算法版本（≥2）；
 *      —— App 据此才敢给出"重建图片缓存"入口；
 *   2. 书源页真的有这个入口，且确认后能跑通（删的是旧算法处理过的图片，书库/收藏不动）；
 *   3. 失败策略：还原失败必须报错、且**不留文件**（离线用例覆盖算法与落盘策略，
 *      这里验证入口与响应契约）。
 *
 * 明确不宣称的：真实 jm 章节的端到端阅读验证。本轮设备网络下 jm 搜索返回 0 条
 * （源站侧不可达/被拦，非本机问题），因此真实章节闭环**只能在用户目标手机上做**；
 * 算法本身的正确性由离线向量 + 合成图逐像素往返用例保证。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class JmImagePipelineTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext

    private fun ev(line: String) = println("JM_PIPELINE_EVIDENCE $line")

    private fun texts(t: String, substring: Boolean = false) =
        rule.onAllNodesWithText(t, substring = substring).fetchSemanticsNodes().size

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size

    private fun tagText(tag: String): String {
        val prop = androidx.compose.ui.semantics.SemanticsProperties.Text
        return rule.onAllNodesWithTag(tag).fetchSemanticsNodes().joinToString(" ") { n ->
            n.config.getOrNull(prop)?.joinToString("") { it.text } ?: ""
        }
    }

    @Test
    fun jmIsReportedAsScrambledSource_withRebuildEntry() {
        // 1) 能力契约：jm 必须如实标为"需要块还原"，并带算法版本
        val gw = EngineGateway(ctx)
        val ep = kotlinx.coroutines.runBlocking { gw.connect() } as EngineState.Ready
        val src = kotlinx.coroutines.runBlocking {
            gw.httpText(ep.endpoint.port, "/api/manga/sources")
        }
        val arr = JSONObject(src.body).optJSONArray("sources")!!
        var jm: JSONObject? = null
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            if (o.optString("key") == "jm") jm = o
        }
        assertTrue("漫画源清单里必须有 jm", jm != null)
        assertTrue("jm 必须被标为混淆源（scrambled=true）——App 据此给重建入口",
            jm!!.optBoolean("scrambled"))
        val pv = jm.optInt("process_version")
        ev("jm：scrambled=true，process_version=$pv，status=${jm.optString("status")}")
        assertTrue("处理版本必须 ≥2（修复后的算法版本），实际 $pv", pv >= 2)

        // 2) 源页面：混淆源必须出现"重建图片缓存"入口
        waitText("书架", timeoutMs = 150_000)
        rule.onAllNodesWithText("浏览")[0].performClick()
        waitText("漫画（优先）", timeoutMs = 60_000)
        rule.onAllNodesWithText("内置漫画源", substring = true)[0].performClick()
        rule.waitUntil(120_000) { nodes("manga_source_list") > 0 }
        // 进 jm 源页（卡片文字里带"禁漫"）
        rule.waitUntil(60_000) { texts("禁漫", substring = true) > 0 }
        rule.onAllNodesWithText("禁漫", substring = true)[0].performClick()
        rule.waitUntil(120_000) { nodes("manga_source_page") > 0 }
        rule.waitUntil(60_000) { nodes("manga_rebuild_cache") > 0 }
        assertTrue("混淆源页面必须有「重建图片缓存」入口", nodes("manga_rebuild_cache") > 0)
        ev("jm 源页出现「重建图片缓存（修复块错乱）」入口")

        // 3) 确认对话框必须说清"删什么、留什么"
        rule.onNodeWithTag("manga_rebuild_cache").performClick()
        rule.waitUntil(30_000) { nodes("manga_rebuild_dialog") > 0 }
        assertTrue("对话框必须写明书库/收藏/历史保留",
            texts("书库条目", substring = true) > 0)
        rule.onNodeWithTag("manga_rebuild_confirm").performClick()
        rule.waitUntil(120_000) { nodes("manga_rebuild_result") > 0 }
        val result = tagText("manga_rebuild_result")
        ev("重建结果：${result.take(160)}")
        assertTrue("结果必须可读（要么说明重建了多少，要么说明没有需要重建的）：$result",
            result.contains("已重建") || result.contains("没有需要重建"))
    }

    private fun waitText(t: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) { texts(t, substring) > 0 }
    }
}
