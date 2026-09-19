package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.security.MessageDigest

/**
 * **覆盖升级一致性**验收（方向基线 §7.2 P1-C"升级后任务和文件一致"、§8.E
 * "同签名覆盖升级保存书库和进度"）。
 *
 * 为什么拆成两段：覆盖安装（`adb install -r`）不能在仪器化进程里对自己做，
 * 必须由外部执行。所以本类分为两个**阶段**，由外部脚本在中间完成安装：
 *
 *   阶段 1 `phase1_setupState`（新版本上造状态）：真实源下载一话并**暂停**任务、
 *            写一条阅读进度、手动改一个内置源文件（模拟用户改动），把证据写进
 *            应用私有目录的 `upgrade-check.json`。
 *   外部：`adb install -d <旧版本 APK>`（降级安装、保留数据）→ 让**旧版本真的运行**
 *            （旧代码会跑自己的种子合并、读同一份书库）→ `adb install -r <新版本 APK>`。
 *   阶段 2 `phase2_verifyAfterUpgrade`：逐项核对升级前后一致：
 *            · 书库条目还在、下载的图片**哈希未变**且仍能解码；
 *            · `_info.json` 仍可解析、章节列表包含那一话；
 *            · **暂停的任务没有擅自复活**（仍为 paused/stopped，磁盘张数不变）；
 *            · 阅读进度（章节 + 页码）原样保留；
 *            · 用户手动改过的源文件**一个字节都没变**（种子合并不得覆盖用户改动）。
 *
 * 只跑阶段 1（或只跑阶段 2）都会给出明确失败信息，不会假装通过。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
// 阶段顺序必须确定：整包运行时 phase1 必须先造状态、phase2 才能核对
@org.junit.FixMethodOrder(org.junit.runners.MethodSorters.NAME_ASCENDING)
class UpgradeConsistencyTest {

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext
    private val source = "mangadex"

    private fun ev(line: String) = println("UPGRADE_EVIDENCE $line")

    private fun evidenceFile() = File(ctx.filesDir, "upgrade-check.json")
    private fun sourcesDir() = File(OfflineStore.runtimeDir(ctx), "sources")

    private fun sha(f: File): String =
        MessageDigest.getInstance("SHA-256").digest(f.readBytes())
            .joinToString("") { "%02x".format(it) }

    private fun isImage(f: File): Boolean {
        if (!f.isFile || f.length() < 1000) return false
        val b = ByteArray(12)
        return try {
            f.inputStream().use { it.read(b) }
            (b[0] == 0xFF.toByte() && b[1] == 0xD8.toByte()) ||
                (b[0] == 0x89.toByte() && b[1] == 'P'.code.toByte()) ||
                (String(b, 0, 4) == "RIFF") || (String(b, 0, 3) == "GIF")
        } catch (t: Throwable) { false }
    }

    private fun decodable(f: File): Boolean {
        val o = android.graphics.BitmapFactory.Options().apply { inJustDecodeBounds = true }
        android.graphics.BitmapFactory.decodeFile(f.absolutePath, o)
        return o.outWidth > 0 && o.outHeight > 0
    }

    private fun chapterDir(comicId: String, chapterId: String) = File(
        OfflineStore.runtimeDir(ctx), "manga/downloads/$source/$comicId/$chapterId")

    // ── 阶段 1：造状态 ──────────────────────────────────────────────────

    @Test
    fun phase1_setupState(): Unit = runBlocking {
        val gateway = EngineGateway(ctx)
        val st = gateway.connect()
        assertTrue("引擎未就绪：$st", st is EngineState.Ready)
        val ep = (st as EngineState.Ready).endpoint

        // 1) 真实源：搜索 → 详情 → 建下载任务（真实链路，不用夹具）
        // 夹具解析：优先缓存、失败才搜索（源站限流不再让本用例成片失败）
        val fx = MangaFixture.pick(gateway, ep.port, source) { ev(it) }
        val comicId = fx.comicId
        val chapterId = fx.chapterId
        ev("选中：$comicId / ${fx.chapterId}")

        val body = JSONObject().put("title", fx.title).put("cover", fx.cover)
            .put("chapters", JSONArray().put(chapterId)).toString()
        val ok = gateway.httpPost(ep.port, "/api/manga/$source/$comicId/download", body)
        assertTrue("创建下载任务应 2xx：HTTP ${ok.code}", ok.code in 200..299)

        // 2) 等真有几张图落盘，然后**暂停**（模拟"用户停了一半，然后升级"）
        val dir = chapterDir(comicId, chapterId)
        var n = 0
        for (i in 0 until 60) {
            delay(2000)
            n = dir.listFiles().orEmpty().count { isImage(it) }
            if (n >= 2) break
        }
        assertTrue("下载应至少落盘 2 张（实际 $n）", n >= 2)
        gateway.httpPost(ep.port, "/api/manga/download/pause?source=$source&cid=$comicId")
        var pausedStatus = ""
        for (i in 0 until 60) {
            delay(1000)
            pausedStatus = taskStatus(gateway, ep.port, comicId)
            if (pausedStatus in listOf("paused", "stopped")) break
        }
        assertTrue("暂停应生效（实际 $pausedStatus）", pausedStatus in listOf("paused", "stopped"))

        // 3) 写阅读进度（第 1 话第 3 页）——升级后必须原样保留
        val hp = gateway.httpPost(ep.port, "/api/manga/history", JSONObject()
            .put("source", source).put("comic_id", comicId)
            .put("idx", 0).put("pos", EngineData.mangaPos(fx.chapterId, 3))
            .put("title", fx.title).toString())
        assertTrue("写进度应成功：HTTP ${hp.code}", hp.ok)

        // 3b) 另造一部**已完成下载**的本地夹具：书库条目 + 进度 + 图片都在本机，
        //     用来验证"覆盖升级保存书库和进度"（真实任务的暂停状态单列一条验证——
        //     注意：任务未完成时**不会**写书库条目，这是设计如此，不是缺陷）
        val fixture = SelfTestComic()
        fixture.create()
        assertTrue("夹具书库条目应存在", fixture.libraryEntryCount() == 1)

        // 4) 模拟用户改动一个内置源文件（升级合并绝不能覆盖用户的东西）
        val userFile = sourcesDir().listFiles { f ->
            f.isFile && f.name.endsWith(".json")
        }?.sortedBy { it.name }?.firstOrNull()
            ?: throw AssertionError("设备上没有源文件可改")
        val userOrig = userFile.readBytes()
        File(ctx.filesDir, "upgrade-check-orig.json").writeBytes(userOrig)
        val jo = JSONObject(userFile.readText(Charsets.UTF_8))
        jo.put("bookSourceComment", "用户自己改过（升级一致性用例）")
        userFile.writeText(jo.toString(), Charsets.UTF_8)
        val userHash = sha(userFile)

        // 4b) 把种子合并标记的**判定逻辑版本**改旧：模拟"这台设备上的标记是旧版判定写的"
        //     （评审点出的真实处境）。这样升级后新版本会**真的重跑一次合并**，
        //     我们才能验证"合并跑过了，而且没碰用户改过的文件"，而不是"没人动过"。
        val marker = File(sourcesDir(), ".seed-applied")
        var mkBefore = 0L
        if (marker.exists()) {
            val mk = JSONObject(marker.readText(Charsets.UTF_8))
            mkBefore = mk.optLong("applied_at")
            mk.put("logic", 1)
            marker.writeText(mk.toString(), Charsets.UTF_8)
        }

        val files = dir.listFiles().orEmpty().filter { isImage(it) }.sortedBy { it.name }
        val evJson = JSONObject()
            .put("created_at", System.currentTimeMillis())
            .put("source", source)
            .put("comic_id", comicId)
            .put("chapter_id", chapterId)
            .put("chapter_label", fx.chapterId)
            .put("title", fx.title)
            .put("images", files.size)
            .put("paused_status", pausedStatus)
            .put("history_pos", EngineData.mangaPos(fx.chapterId, 3))
            .put("user_source_file", userFile.name)
            .put("user_source_hash", userHash)
            .put("fixture_comic", SelfTestComic.CH1_ID.let { "__selftest__" })
            .put("fixture_title", SelfTestComic.TITLE)
            .put("fixture_chapter1", SelfTestComic.CH1_ID)
            .put("fixture_images", 3)     // 第 1 话 2 张 + 第 2 话 1 张
            .put("marker_applied_at_before", mkBefore)
            .put("hashes", JSONObject().apply {
                files.forEach { put(it.name, sha(it)) }
            })
        evidenceFile().writeText(evJson.toString(), Charsets.UTF_8)
        ev("阶段 1 完成：$comicId 已落盘 ${files.size} 张，任务=$pausedStatus，" +
            "进度=第 1 话 P3，用户改过 ${userFile.name}")
        gateway.stopEngine()
    }

    // ── 阶段 2：升级后逐项核对 ──────────────────────────────────────────

    @Test
    fun phase2_verifyAfterUpgrade(): Unit = runBlocking {
        assertTrue("阶段 1 的证据不在（需先跑 phase1_setupState）：${evidenceFile()}",
            evidenceFile().exists())
        val e = JSONObject(evidenceFile().readText(Charsets.UTF_8))
        val comicId = e.getString("comic_id")
        val chapterId = e.getString("chapter_id")
        val before = e.getJSONObject("hashes")
        val nBefore = e.getInt("images")

        val gateway = EngineGateway(ctx)
        val st = gateway.connect()
        assertTrue("升级后引擎应能就绪：$st", st is EngineState.Ready)
        val ep = (st as EngineState.Ready).endpoint
        ev("升级后引擎就绪 port=${ep.port} instance=${ep.instanceId.take(8)}")

        // 1) 书库条目还在（已完成下载的夹具；未完成的任务不写书库条目，见阶段 1 注释）
        val lib = gateway.httpText(ep.port, "/api/manga/library")
        assertTrue("书库应能读到：HTTP ${lib.code}", lib.ok)
        assertTrue("升级后书库必须仍有已下载的漫画（夹具 ${e.getString("fixture_title")}）",
            lib.body.contains(e.getString("fixture_comic")))
        ev("书库条目仍在（${e.getString("fixture_title")}）")

        // 2) 图片：一张不多不少、**哈希未变**、仍能解码
        val dir = chapterDir(comicId, chapterId)
        val files = dir.listFiles().orEmpty().filter { isImage(it) }.sortedBy { it.name }
        assertTrue("升级后图片不得丢失（升级前 $nBefore 张，现在 ${files.size} 张）",
            files.size >= nBefore)
        for (name in before.keys()) {
            val f = File(dir, name)
            assertTrue("升级后图片必须还在：$name", f.isFile)
            assertEquals("升级后图片内容不得变化：$name", before.getString(name), sha(f))
            assertTrue("升级后图片必须仍能解码：$name", decodable(f))
        }
        ev("图片 ${files.size} 张：哈希一致、逐张可解码")

        // 3) _info.json 仍可解析、章节列表包含那一话
        val info = File(OfflineStore.runtimeDir(ctx), "manga/downloads/$source/$comicId/_info.json")
        assertTrue("_info.json 必须还在", info.isFile)
        val infoJson = runCatching { JSONObject(info.readText(Charsets.UTF_8)) }
            .getOrElse { throw AssertionError("_info.json 升级后不可解析：${it.message}") }
        val chapters = infoJson.optJSONArray("chapters") ?: JSONArray()
        var found = false
        for (i in 0 until chapters.length()) {
            if (chapters.optJSONObject(i)?.optString("id") == chapterId) found = true
        }
        assertTrue("_info.json 的章节列表必须仍包含 $chapterId", found)
        ev("_info.json 可解析，章节列表完整")

        // 4) **暂停的任务不得擅自复活**（升级不该等于"用户点了继续"）
        var status = taskStatus(gateway, ep.port, comicId)
        delay(3000)                       // 给"复活路径"留出时间
        status = taskStatus(gateway, ep.port, comicId)
        assertTrue("升级后任务不得自己跑起来（实际 $status）",
            status in listOf("paused", "stopped", "idle"))
        val nAfter = dir.listFiles().orEmpty().count { isImage(it) }
        assertEquals("升级后磁盘张数不应变化（暂停状态）", nBefore, nAfter)
        ev("任务仍为 $status，磁盘张数不变（$nAfter）")

        // 4b) 夹具（已完成下载）的图片在升级后仍可解码、`_library.json` 仍可解析
        val fixture = SelfTestComic()
        val fixDir = File(OfflineStore.runtimeDir(ctx),
            "manga/downloads/${fixture.sourceKey}/${e.getString("fixture_comic")}")
        var fixImgs = 0
        for (d in fixDir.listFiles().orEmpty()) {
            if (!d.isDirectory) continue
            for (f in d.listFiles().orEmpty()) if (f.isFile && f.length() > 100) {
                assertTrue("升级后夹具图片必须仍能解码：${f.name}", decodable(f))
                fixImgs++
            }
        }
        assertEquals("夹具图片数应一致", e.getInt("fixture_images"), fixImgs)
        ev("夹具图片 $fixImgs 张全部可解码")

        // 5) 阅读进度原样保留
        val hr = gateway.httpText(ep.port, "/api/manga/history")
        val arr = JSONObject(hr.body).optJSONArray("history") ?: JSONArray()
        var pos = ""
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            if (o.optString("comic_id") == comicId) pos = o.optString("pos")
        }
        assertEquals("阅读进度必须原样保留", e.getString("history_pos"), pos)
        ev("阅读进度保留：$pos")

        // 6) 用户改过的源文件**一个字节都没变**（种子合并不得覆盖用户改动）
        val uf = File(sourcesDir(), e.getString("user_source_file"))
        assertTrue("用户改过的源文件必须还在：${uf.name}", uf.isFile)
        assertEquals("用户改过的源文件不得被升级合并覆盖",
            e.getString("user_source_hash"), sha(uf))
        ev("用户改过的源文件未被覆盖：${uf.name}")

        // 7) 新版本确实跑过种子合并（否则第 6 条只是"没人动过"）
        val marker = File(sourcesDir(), ".seed-applied")
        assertTrue("应有种子合并标记", marker.exists())
        val mk = JSONObject(marker.readText(Charsets.UTF_8))
        // 升级专属断言只在**外部真的做了覆盖安装**时强制：
        // 整包运行时（没有中间安装步骤）合并是幂等的、不会再跑，这不是缺陷。
        // 判据是运行参数 -e upgradeHarness 1（由覆盖升级脚本传入），
        // 不能靠"版本号变了"——覆盖升级前后版本号可能相同（降级再升回来）。
        val harness = InstrumentationRegistry.getArguments()
            .getString("upgradeHarness") == "1"
        if (harness) {
            assertTrue("升级后应重新跑过种子合并（阶段 1 把标记 logic 改成 1；" +
                "applied_at=${mk.optLong("applied_at")} vs 阶段 1 时间=${e.getLong("created_at")}）",
                mk.optLong("applied_at") > e.getLong("created_at"))
            assertEquals("合并应记录当前判定逻辑版本", 2, mk.optInt("logic"))
            assertTrue("这一轮合并必须**看见并跳过**用户改过的文件（modified_skipped=" +
                "${mk.optInt("modified_skipped")}）", mk.optInt("modified_skipped") >= 1)
            ev("种子合并在升级后真的跑过：logic=${mk.optInt("logic")}，" +
                "出厂未改动 ${mk.optInt("untouched")}，用户改过而跳过 ${mk.optInt("modified_skipped")}")
        } else {
            ev("注意：本次**没有**经过覆盖安装（未传 -e upgradeHarness 1）→ " +
                "只核对数据一致性，升级专属断言（合并重跑）本次未验证；" +
                "marker logic=${mk.optInt("logic")} applied_at=${mk.optLong("applied_at")}")
        }

        // 清理：移除书库条目并删文件（不留测试下载）
        // 还原用例自己改过的源文件（阶段 1 备份了原文），别把设备留在被改状态
        val uf2 = File(sourcesDir(), e.getString("user_source_file"))
        val origBackup = File(ctx.filesDir, "upgrade-check-orig.json")
        if (origBackup.isFile) {
            uf2.writeBytes(origBackup.readBytes())
            origBackup.delete()
            ev("已还原用例改过的源文件：${uf2.name}")
        }

        val rm = gateway.httpDelete(ep.port, "/api/manga/library/$source/$comicId?files=1")
        assertTrue("清理应 200：HTTP ${rm.code}", rm.ok)
        fixture.cleanup()
        assertTrue("夹具未清理干净", !fixture.exists())
        evidenceFile().delete()
        gateway.stopEngine()
        ev("阶段 2 通过并清理完成")
    }

    private suspend fun taskStatus(gw: EngineGateway, port: Int, comicId: String): String {
        val r = gw.httpText(port, "/api/manga/download/status?source=$source&cid=$comicId")
        return runCatching { JSONObject(r.body).optString("status") }.getOrDefault("")
    }
}
