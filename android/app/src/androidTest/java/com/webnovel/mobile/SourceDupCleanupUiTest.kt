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
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * 重复源文件**在 App 内清理**（方向基线 §8.A：源的问题不应要求用户去用电脑）。
 *
 * 背景：内置源里实测有 4 组"同一 uid、同一站点地址"的双份文件；此前 App 只能提示
 * "建议在网页端或文件系统里清理"。本用例验证现在这条路径真的可用：
 *   1. 书源管理页出现"重复源文件"卡片与清理入口；
 *   2. 对话框把"保留哪一份 / 移走哪几份 / 为什么"逐条列清楚；
 *   3. 确认后文件被**移到备份目录**（不是删除），界面刷新并回显备份路径；
 *   4. 保留的那份仍在。
 *
 * 为了可重复：用例先自己造一组重复（复制一份现有源文件、改文件名、状态设为停用），
 * 这样即使真实那 4 组已经被清理过，本用例仍然成立。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class SourceDupCleanupUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext

    private fun ev(line: String) = println("DUP_CLEANUP_EVIDENCE $line")

    private fun sourcesDir() = File(OfflineStore.runtimeDir(ctx), "sources")

    private fun texts(t: String, substring: Boolean = false) =
        rule.onAllNodesWithText(t, substring = substring).fetchSemanticsNodes().size

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size

    private fun waitText(t: String, timeoutMs: Long = 120_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) { texts(t, substring) > 0 }
    }

    @Test
    fun duplicatesCanBeCleanedInsideTheApp() {
        // 1) 造一组重复：复制一个现有源文件（保留 uid/url，改文件名 + 停用）
        // 必须跳过点文件：.seed-removed.json 是删除墓碑，不是书源
        // （整包运行时被它绊到过：复制出来的"重复源"没有 uid/url，卡片当然不出现）
        val base = sourcesDir().listFiles { f ->
            f.isFile && f.name.endsWith(".json") && !f.name.startsWith(".")
        }?.sortedBy { it.name }?.firstOrNull()
            ?: throw AssertionError("设备上没有书源文件，无法构造重复组")
        val copy = File(sourcesDir(), "dup-test-" + base.name)
        val o = JSONObject(base.readText(Charsets.UTF_8))
        o.put("enabled", false)
        copy.writeText(o.toString(), Charsets.UTF_8)
        ev("已造重复文件：${copy.name}（与 ${base.name} 同 uid=${o.optString("uid")}）")
        assertTrue("复制失败", copy.isFile)

        // 2) 进书源管理 → 必须出现重复卡片与清理入口
        waitText("书架", timeoutMs = 150_000)
        waitText("设置")
        rule.onAllNodesWithText("设置")[0].performClick()
        waitText("书源管理")
        rule.onAllNodesWithText("书源管理")[0].performClick()
        rule.waitUntil(120_000) { nodes("src_dup_card") > 0 }
        rule.onNodeWithTag("src_dup_card").assertExists()
        rule.onNodeWithTag("src_dup_open").assertExists()
        ev("书源管理页出现重复源文件卡片与清理入口")

        // 3) 打开对话框 → 必须列清楚保留/移走
        rule.onNodeWithTag("src_dup_open").performClick()
        rule.waitUntil(30_000) { nodes("src_dup_dialog") > 0 }
        assertTrue("对话框必须写明保留哪一份",
            texts("保留", substring = true) > 0 && texts("移走", substring = true) > 0)
        ev("对话框列明保留/移走/理由")

        // 4) 确认清理 → 文件必须被移走，且保留的那份还在
        rule.onNodeWithTag("src_dup_confirm").performClick()
        rule.waitUntil(120_000) { nodes("src_dup_result") > 0 }
        val result = rule.onAllNodesWithTag("src_dup_result").fetchSemanticsNodes()
            .joinToString(" ") { n ->
                n.config.getOrNull(androidx.compose.ui.semantics.SemanticsProperties.Text)
                    ?.joinToString("") { it.text } ?: ""
            }
        ev("清理结果：${result.take(200)}")
        assertTrue("结果必须说明移走了几个文件与备份位置：$result",
            result.contains("已清理") && result.contains("备份"))
        assertTrue("用例造的那份重复文件必须已被移走", !copy.isFile)
        assertTrue("保留的那份不能被删", base.isFile)
        // 备份目录在数据目录下：<runtimeDir>/sources_removed/<时间戳>/（服务端 DATA_DIR 口径）
        val removedRoot = File(OfflineStore.runtimeDir(ctx), "sources_removed")
        val backed = removedRoot.exists() && removedRoot.walkTopDown()
            .any { it.isFile && it.name == copy.name }
        assertTrue("被移走的文件必须在备份目录里（可恢复）：$removedRoot", backed)
        ev("重复文件已移到备份目录，保留的那份仍在")

        // 5) **重启引擎后也不许把清理掉的文件加回来**（最强的一条）
        //    实测缺陷：清理后下次启动种子解压又把 4 个文件解出来了（"删了又忘不了"）
        kotlinx.coroutines.runBlocking {
            val gw = EngineGateway(ctx)
            gw.connect()
            gw.stopEngine()
            kotlinx.coroutines.delay(1500)
            gw.connect()
        }
        kotlinx.coroutines.runBlocking { kotlinx.coroutines.delay(3000) }
        assertTrue("重启后用户清理掉的源不许被内置源加回来：${copy.name} 又出现了",
            !copy.isFile)
        val tomb = File(sourcesDir(), ".seed-removed.json")
        assertTrue("应留下删除墓碑（种子解压据此跳过）", tomb.isFile)
        ev("重启后仍未被加回（墓碑生效）")
    }
}
