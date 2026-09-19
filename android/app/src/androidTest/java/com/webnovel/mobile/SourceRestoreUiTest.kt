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
 * 清理出来的源文件**能在 App 里恢复**（清理只移动不删除，但备份在应用私有目录里，
 * 用户点不到——等于"清错了找不回来"）。
 *
 * 流程（全部在 App 内点出来，不依赖电脑）：
 *   1. 造一组重复 → 书源管理 → 清理（文件被移走、留墓碑）；
 *   2. 页面出现"已清理/删除的源文件：N 个（可恢复）"；
 *   3. 点「恢复被清理的源」→ 确认 → 文件回到书源目录、墓碑解除、结果回显。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class SourceRestoreUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext

    private fun ev(line: String) = println("RESTORE_EVIDENCE $line")

    private fun sourcesDir() = File(OfflineStore.runtimeDir(ctx), "sources")

    private fun texts(t: String, substring: Boolean = false) =
        rule.onAllNodesWithText(t, substring = substring).fetchSemanticsNodes().size

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size

    private fun tagText(tag: String): String {
        val prop = androidx.compose.ui.semantics.SemanticsProperties.Text
        return rule.onAllNodesWithTag(tag).fetchSemanticsNodes().joinToString(" ") { n ->
            n.config.getOrNull(prop)?.joinToString("") { it.text } ?: ""
        }
    }

    private fun waitText(t: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) { texts(t, substring) > 0 }
    }

    @Test
    fun cleanedSourceCanBeRestoredInApp() {
        // 1) 造一组重复（跳过点文件：.seed-removed.json 是墓碑，不是书源）
        val base = sourcesDir().listFiles { f ->
            f.isFile && f.name.endsWith(".json") && !f.name.startsWith(".")
        }?.sortedBy { it.name }?.firstOrNull()
            ?: throw AssertionError("设备上没有书源文件")
        val copy = File(sourcesDir(), "restore-test-" + base.name)
        val o = JSONObject(base.readText(Charsets.UTF_8)).put("enabled", false)
        copy.writeText(o.toString(), Charsets.UTF_8)
        ev("已造重复文件：${copy.name}")

        // 2) 进书源管理 → 清理（先把它移走）
        waitText("书架", timeoutMs = 150_000)
        rule.onAllNodesWithText("设置")[0].performClick()
        waitText("书源管理")
        rule.onAllNodesWithText("书源管理")[0].performClick()
        rule.waitUntil(120_000) { nodes("src_dup_open") > 0 }
        rule.onNodeWithTag("src_dup_open").performClick()
        rule.waitUntil(30_000) { nodes("src_dup_confirm") > 0 }
        rule.onNodeWithTag("src_dup_confirm").performClick()
        rule.waitUntil(120_000) { nodes("src_dup_result") > 0 }
        assertTrue("清理后文件应被移走", !copy.isFile)
        ev("已清理：${copy.name} 被移到备份目录")

        // 3) 必须出现"可恢复"入口
        rule.waitUntil(120_000) { nodes("src_restore_card") > 0 }
        assertTrue("必须有恢复入口（否则清错了找不回来）", nodes("src_restore_open") > 0)
        ev("书源管理页出现「恢复被清理的源」入口")

        // 4) 恢复 → 文件回来、墓碑解除
        rule.onNodeWithTag("src_restore_open").performClick()
        rule.waitUntil(30_000) { nodes("src_restore_dialog") > 0 }
        rule.onNodeWithTag("src_restore_confirm").performClick()
        rule.waitUntil(120_000) { nodes("src_restore_result") > 0 }
        val result = tagText("src_restore_result")
        ev("恢复结果：${result.take(160)}")
        assertTrue("结果必须说明恢复了几份：$result", result.contains("已恢复"))
        assertTrue("文件必须真的回到书源目录", copy.isFile)
        val tomb = File(sourcesDir(), ".seed-removed.json")
        val tombText = if (tomb.isFile) tomb.readText(Charsets.UTF_8) else ""
        assertTrue("恢复后必须解除墓碑（否则种子逻辑一直当它被删过）",
            !tombText.contains(copy.name))
        ev("文件已恢复且墓碑已解除")

        // 清理：把用例造的这份删掉，别留在设备上
        copy.delete()
    }
}
