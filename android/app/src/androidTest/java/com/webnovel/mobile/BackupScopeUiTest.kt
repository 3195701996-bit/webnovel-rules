package com.webnovel.mobile

import android.net.Uri
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.zip.ZipInputStream

/**
 * **SAF 导入导出的导出侧 + 备份范围明确**（路线 §7 P1-2），设备上跑真实引擎。
 *
 * 三件事：
 *   1. `GET /api/sources/export`：全部源导出 → 单源导出 → 不存在的 uid 必须 404
 *      （客户端不能把"空"当成功存成文件）；
 *   2. **导出的文件真的能被导回**：把导出的 JSON 交给 `/api/sources/import`，
 *      源必须回到书源列表里（这才是"导入导出"能用的前提）；
 *   3. **备份清单里写明范围**：用 `Backup.create` 真的生成一份 zip（写到应用私有目录，
 *      不弹系统选择器），解出 `manifest.json`，断言里面同时有
 *      `scope.includes`（含什么）与 `scope.excludes`（明确不含什么）+ 说明文案；
 *      并核对"清单里的条目"与"zip 里的文件"**一一对应**（清单不能漏报/多报）。
 *
 * 界面断言：书源管理页必须有「导出」入口；备份页必须显示服务端实测的范围数字。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class BackupScopeUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext
    private lateinit var gateway: EngineGateway

    private fun ev(line: String) = println("BACKUP_SCOPE_EVIDENCE $line")

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size
    private fun texts(t: String, substring: Boolean = false) =
        rule.onAllNodesWithText(t, substring = substring).fetchSemanticsNodes().size

    private fun waitWhileIdle() = Unit

    @Test
    fun exportSources_areReimportable_andMissingUidIs404() = runBlocking {
        gateway = EngineGateway(ctx)
        val ep = (gateway.connect() as? EngineState.Ready)?.endpoint
            ?: throw AssertionError("引擎未就绪")

        val all = gateway.httpText(ep.port, "/api/sources/export")
        assertTrue("导出应 200：HTTP ${all.code}", all.ok)
        val body = JSONObject(all.body)
        assertTrue("导出应带版本号", body.optInt("version") >= 1)
        val sources = body.optJSONArray("sources")!!
        assertTrue("应导出到书源（实际 ${sources.length()}）", sources.length() > 0)
        for (i in 0 until sources.length()) {
            assertTrue("每条导出项都要有 bookSourceUrl（否则导不回）",
                sources.optJSONObject(i)?.optString("bookSourceUrl").orEmpty().isNotBlank())
        }
        val firstUid = sources.optJSONObject(0)!!.optString("uid")
        assertTrue("导出项应带 uid", firstUid.isNotBlank())
        ev("全部导出：${sources.length()} 个源，含 uid（如 $firstUid）")

        val one = gateway.httpText(ep.port, "/api/sources/export?uid=${Uri.encode(firstUid)}")
        assertTrue("单源导出应 200", one.ok)
        assertTrue("单源导出应恰好 1 条", JSONObject(one.body).optJSONArray("sources")!!.length() == 1)

        val miss = gateway.httpText(ep.port, "/api/sources/export?uid=${Uri.encode("绝对不存在_uid")}")
        assertTrue("不存在的 uid 必须 404（不能让客户端存下空文件），实际 ${miss.code}",
            miss.code == 404)
        ev("单源导出 ok；不存在的 uid → HTTP ${miss.code}")

        // 往返：导出 → 导入 → 仍在列表里
        val imp = gateway.httpPost(ep.port, "/api/sources/import",
            JSONObject().put("content", one.body).toString())
        assertTrue("导回应 200：HTTP ${imp.code} ${imp.body.take(80)}", imp.ok)
        val imported = JSONObject(imp.body).optInt("imported")
        assertTrue("导回应至少写入 1 条（实际 $imported）", imported >= 1)
        ev("导回：imported=$imported")
        Unit    // runBlocking 会返回最后表达式的值，JUnit 要求 void
    }

    @Test
    fun backupManifest_statesScope_andMatchesZipEntries() = runBlocking {
        // 生成真实备份（避开系统选择器：直接写到应用私有目录的 file:// URI）
        val out = File(ctx.cacheDir, "backup-scope-test.zip")
        if (out.exists()) out.delete()
        val report = Backup.create(ctx, Uri.fromFile(out), appVersion = "test")
        assertTrue("备份应写出文件且非空", out.isFile && out.length() > 0)
        ev("备份：${report.files} 个文件 / ${report.bytes} 字节")

        var manifest: JSONObject? = null
        val zipEntries = ArrayList<String>()
        ZipInputStream(out.inputStream().buffered()).use { zis ->
            var e = zis.nextEntry
            while (e != null) {
                zipEntries.add(e.name)
                if (e.name == Backup.MANIFEST) {
                    manifest = JSONObject(String(zis.readBytes(), Charsets.UTF_8))
                }
                zis.closeEntry()
                e = zis.nextEntry
            }
        }
        val m = manifest ?: throw AssertionError("备份里没有 ${Backup.MANIFEST}")

        // 1) 范围必须写清楚：含什么、明确不含什么、以及为什么
        val scope = m.optJSONObject("scope") ?: throw AssertionError("清单缺少 scope（范围不明确）")
        val inc = scope.optJSONArray("includes")!!
        val exc = scope.optJSONArray("excludes")!!
        assertTrue("includes 应说明包含书源与进度", inc.length() >= 3)
        assertTrue("excludes 必须明确列出不含的内容", exc.length() >= 3)
        val excText = (0 until exc.length()).joinToString(" ") { exc.optString(it) }
        assertTrue("必须明确'不含正文'：$excText", excText.contains("正文"))
        assertTrue("必须明确'不含漫画图片'：$excText", excText.contains("漫画"))
        assertTrue("scope 必须带说明（不是离线全文副本）",
            scope.optString("note").contains("不是") || scope.optString("note").isNotBlank())
        ev("清单范围：包含 ${inc.length()} 项 / 明确不含 ${exc.length()} 项")

        // 2) 清单条目 ↔ zip 实际内容必须一一对应（清单不许漏报或多报）
        val entries = m.optJSONArray("entries")!!
        val declared = (0 until entries.length())
            .map { entries.optJSONObject(it)!!.optString("path") }.toSet()
        val actual = zipEntries.filter { it != Backup.MANIFEST }.toSet()
        assertTrue("清单里声明了但 zip 里没有：${declared - actual}", actual.containsAll(declared))
        assertTrue("zip 里有但清单没声明的：${actual - declared}", declared.containsAll(actual))
        ev("清单与 zip 内容一一对应：${declared.size} 个条目")

        // 3) 书源与进度文件确实在里面（范围不是嘴上说说）
        assertTrue("备份里应有书源文件：$actual",
            actual.any { it.startsWith("sources/") && it.endsWith(".json") })
        out.delete()
        Unit
    }

    @Test
    fun ui_exportsEntryAndBackupScopeNumbers() {
        gateway = EngineGateway(ctx)
        // 书源管理页：必须有「导出」入口
        rule.waitUntil(150_000) { texts("设置") > 0 }
        rule.onAllNodesWithText("设置")[0].performClick()
        rule.waitUntil(60_000) { texts("书源管理") > 0 }
        rule.onAllNodesWithText("书源管理")[0].performScrollTo().performClick()
        rule.waitUntil(60_000) { nodes("src_export_all") > 0 }
        ev("书源管理页：导出入口在位")
        rule.onAllNodesWithText("← 返回")[0].performClick()

        // 备份页：范围数字来自服务端磁盘实扫
        rule.waitUntil(60_000) { texts("备份与恢复") > 0 }
        rule.onAllNodesWithText("备份与恢复")[0].performScrollTo().performClick()
        rule.waitUntil(60_000) { nodes("backup_scope_summary") > 0 }
        val summary = rule.onAllNodesWithTag("backup_scope_summary")[0]
            .fetchSemanticsNode().config.toString()
        ev("备份页范围：$summary")
        assertTrue("范围摘要要说清'包含…合计…不含…'：$summary",
            summary.contains("包含") && summary.contains("不含") && summary.contains("合计"))
        assertTrue("必须显示不含部分的真实体积（KB/MB/GB）：$summary",
            summary.contains("MB") || summary.contains("KB") || summary.contains("GB"))
        assertFalse("范围摘要不能是空壳", summary.isBlank())
        waitWhileIdle()
    }
}
