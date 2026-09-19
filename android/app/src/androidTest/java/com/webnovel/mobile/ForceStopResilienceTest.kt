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
import org.junit.FixMethodOrder
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.MethodSorters
import java.io.File
import java.security.MessageDigest

/**
 * **系统强杀进程**后的任务与文件一致性（方向基线 §7.2 P1-C"杀进程"）。
 *
 * 与"重启引擎"不是同一条路径：`am force-stop` 是系统级强杀——进程直接消失，
 * Python 引擎连同下载线程一起被砍，没有收尾机会。这里要回答三件事：
 *   1. 磁盘上**不许留下半截/损坏文件**（逐张魔数 + 解码），也不许留 `.tmp` 残渣；
 *   2. 任务状态必须**诚实**（不能谎报 done），`_info.json` 仍可解析；
 *   3. 重新拉起 App 后**能接着下**（不需要用户重新添加），且最终文件完整。
 *
 * 强杀不能由仪器化进程自己做（进程会连测试一起被杀），所以和覆盖升级用例同样的做法：
 * 两阶段 + 外部执行 `adb shell am force-stop`，用 `-e forceStopHarness 1` 标记
 * "确实发生过强杀"，据此强制强杀专属断言；整包运行时只做数据一致性核对并**显式说明**。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
@FixMethodOrder(MethodSorters.NAME_ASCENDING)
class ForceStopResilienceTest {

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext
    private val source = "mangadex"

    private fun ev(line: String) = println("FORCESTOP_EVIDENCE $line")

    private fun evidenceFile() = File(ctx.filesDir, "forcestop-check.json")

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

    // ── 阶段 1：起一个真实下载，把现场记录下来交给外部强杀 ──────────────

    @Test
    fun phase1_startDownloadAndRecord(): Unit = runBlocking {
        val gateway = EngineGateway(ctx)
        val st = gateway.connect()
        assertTrue("引擎未就绪：$st", st is EngineState.Ready)
        val ep = (st as EngineState.Ready).endpoint

        // 夹具解析：优先缓存、失败才搜索（源站限流不再让本用例成片失败）
        val fx = MangaFixture.pick(gateway, ep.port, source) { ev(it) }
        val comicId = fx.comicId
        val chapterId = fx.chapterId
        val body = JSONObject().put("title", fx.title).put("cover", fx.cover)
            .put("chapters", JSONArray().put(chapterId)).toString()
        val ok = gateway.httpPost(ep.port, "/api/manga/$source/$comicId/download", body)
        assertTrue("创建下载任务应 2xx：HTTP ${ok.code}", ok.code in 200..299)

        // 等几张真图落盘，但**不要**等它跑完：要的就是"下到一半被系统强杀"
        val dir = chapterDir(comicId, chapterId)
        var n = 0
        for (i in 0 until 60) {
            delay(1500)
            n = dir.listFiles().orEmpty().count { isImage(it) }
            if (n >= 3) break
        }
        // ≥1 张即可：这里只要求"磁盘上确实有东西"，具体张数由阶段 2 与外部强杀共同决定
        assertTrue("强杀前应已有图片落盘（实际 $n）", n >= 1)
        val status = taskStatus(gateway, ep.port, comicId)
        val files = dir.listFiles().orEmpty().filter { isImage(it) }.sortedBy { it.name }
        val evJson = JSONObject()
            .put("created_at", System.currentTimeMillis())
            .put("source", source).put("comic_id", comicId).put("chapter_id", chapterId)
            .put("title", fx.title).put("chapter_label", fx.chapterId)
            .put("images", files.size).put("status", status)
            .put("hashes", JSONObject().apply { files.forEach { put(it.name, sha(it)) } })
        evidenceFile().writeText(evJson.toString(), Charsets.UTF_8)
        ev("阶段 1 已记录现场：$comicId 已落盘 ${files.size} 张，任务=$status")

        // 关键：**保持在下载进行中被强杀**。仪器化进程就是 App 进程，一旦本方法返回，
        // 进程可能很快被回收，强杀就变成"杀一个已经停下的进程"，测不到真正的中途强杀。
        // 所以这里打印 READY 后继续等待，由外部在这段时间内执行 am force-stop
        // （结果是本次仪器化运行以"进程被杀"结束——这是**预期**，不是缺陷；
        //   强杀后的核对由 phase2 完成）。
        // 只有覆盖强杀脚本（-e forceStopHarness 1）才需要在这里"等被杀"；
        // 整包运行时没人来杀，白等 90 秒只会拖慢套件（实测）。
        if (InstrumentationRegistry.getArguments().getString("forceStopHarness") == "1") {
            ev("READY_TO_BE_FORCE_STOPPED（外部执行 adb shell am force-stop com.webnovel.mobile）")
            val t0 = System.currentTimeMillis()
            while (System.currentTimeMillis() - t0 < 90_000) {
                delay(1000)
            }
            ev("等待结束（若外部没有强杀，说明本次没验证到中途强杀）")
        } else {
            ev("非强杀模式：不起等待（整包运行时由后面的阶段核对数据一致性即可）")
        }
    }

    // ── 阶段 2：强杀之后核对 + 续传 ─────────────────────────────────────

    @Test
    fun phase2_verifyAfterForceStop_andResume(): Unit = runBlocking {
        assertTrue("阶段 1 的证据不在（需先跑 phase1_startDownloadAndRecord）：${evidenceFile()}",
            evidenceFile().exists())
        val e = JSONObject(evidenceFile().readText(Charsets.UTF_8))
        val comicId = e.getString("comic_id")
        val chapterId = e.getString("chapter_id")
        val before = e.getJSONObject("hashes")
        val harness = InstrumentationRegistry.getArguments().getString("forceStopHarness") == "1"

        val dir = chapterDir(comicId, chapterId)
        // 1) 强杀之后、**重新拉起之前**：磁盘上的东西必须仍然干净
        val leftovers = dir.listFiles().orEmpty().filter { it.name.endsWith(".tmp") }
        assertEquals("强杀不得留下 .tmp 残渣（实际 ${leftovers.map { it.name }}）",
            0, leftovers.size)
        val now = dir.listFiles().orEmpty().filter { isImage(it) }
        for (name in before.keys()) {
            val f = File(dir, name)
            assertTrue("强杀前已落盘的图片必须还在：$name", f.isFile)
            assertEquals("强杀不得改动已落盘的图片：$name", before.getString(name), sha(f))
        }
        for (f in now) {
            assertTrue("强杀后每张图都必须能解码：${f.name}（${f.length()} 字节）", decodable(f))
        }
        ev("强杀后磁盘：${now.size} 张图片全部完整可解码，无 .tmp 残渣")

        // 2) 重新拉起（等价于用户重新点图标）→ 引擎必须能起来
        val gateway = EngineGateway(ctx)
        val st = gateway.connect()
        assertTrue("强杀后应能重新拉起引擎：$st", st is EngineState.Ready)
        val ep = (st as EngineState.Ready).endpoint
        ev("强杀后重新拉起：引擎就绪 port=${ep.port}")

        // 3) 任务状态必须诚实：不许谎报 done（它没下完）
        val status = taskStatus(gateway, ep.port, comicId)
        val total = taskImagesTotal(gateway, ep.port, comicId)
        val done = taskImagesDone(gateway, ep.port, comicId)
        ev("强杀后任务：status=$status images_done=$done images_total=$total")
        assertTrue("强杀后任务状态必须明确（实际「$status」）",
            status in listOf("running", "queued", "paused", "stopped", "idle", "error", "done"))
        if (total > 0 && now.size < total) {
            assertTrue("没下完就不许标 done（本地 ${now.size} / 共 $total 张）", status != "done")
        }
        if (harness) {
            // 中途强杀的证据：记录现场之后下载**仍在推进**（说明强杀发生在下载过程中），
            // 而且没下完就跑掉了（没有谎报 done）
            assertTrue("强杀专属断言：记录现场后又推进了（记录 ${e.getInt("images")} 张 →" +
                "强杀后 ${now.size} 张），说明强杀发生在下载过程中",
                now.size > e.getInt("images"))
            assertTrue("强杀专属断言：没下完就不许标 done（本地 ${now.size} / 共 $total）",
                status != "done")
            // 关键：磁盘上的任务记录必须被**重新装载**（否则用户界面上任务凭空消失、
            // 也无法点"继续"）。这曾经是真实缺陷：手机端走 start() 路径时
            // RuntimeController 的初始化钩子被早退吞掉，recover_tasks 从来没跑过。
            assertTrue("强杀后任务记录必须能被装载（可继续），实际状态「$status」" +
                "——idle 表示记录没装载（用户会看到任务消失）",
                status in listOf("paused", "stopped", "queued", "running", "error"))
        } else {
            ev("注意：本次**没有**经过系统强杀（未传 -e forceStopHarness 1）→ " +
                "只做数据一致性核对，强杀专属断言本次未验证")
        }

        // 4) `_info.json` 仍可解析（续传依赖它）
        val info = File(OfflineStore.runtimeDir(ctx), "manga/downloads/$source/$comicId/_info.json")
        assertTrue("_info.json 必须还在（续传依赖）", info.isFile)
        val infoJson = runCatching { JSONObject(info.readText(Charsets.UTF_8)) }
            .getOrElse { throw AssertionError("_info.json 强杀后不可解析：${it.message}") }
        val chapters = infoJson.optJSONArray("chapters") ?: JSONArray()
        var found = false
        for (i in 0 until chapters.length()) {
            if (chapters.optJSONObject(i)?.optString("id") == chapterId) found = true
        }
        assertTrue("_info.json 的章节列表必须仍包含 $chapterId", found)
        ev("_info.json 可解析、章节完整")

        // 5) 续传：重新点"继续"就能接着下（不必重新添加任务）
        gateway.httpPost(ep.port, "/api/manga/download/resume?source=$source&cid=$comicId")
        var grew = false
        var status2 = ""
        for (i in 0 until 45) {
            delay(2000)
            if (dir.listFiles().orEmpty().count { isImage(it) } > now.size) { grew = true; break }
            status2 = taskStatus(gateway, ep.port, comicId)
            if (status2 == "error" && i > 4) break
            if (status2 == "done") break
        }
        val nEnd = dir.listFiles().orEmpty().count { isImage(it) }
        ev("续传：张数 ${now.size} → $nEnd，status=$status2")
        // 口径：只有在"确实还有没下完的内容"时才要求继续增长。
        // 整包运行时（没有外部强杀）下载可能早就跑完了，此时"没有增长"是正常的，
        // 不能当成缺陷（实测被整包运行抓到一次假失败）。
        val alreadyComplete = status2 == "done" || (total > 0 && nEnd >= total)
        assertTrue("强杀后重新拉起应能续传或已完成（status=$status2，本地 $nEnd/$total）",
            grew || alreadyComplete)
        if (!grew && alreadyComplete) {
            ev("本次没有剩余内容可续传（任务已完成，$nEnd/$total）——如实记录，不算失败")
        }

        // 6) 续传写下的新文件同样必须完整
        for (f in dir.listFiles().orEmpty().filter { isImage(it) }) {
            assertTrue("续传后每张图都必须能解码：${f.name}", decodable(f))
        }
        ev("续传后 ${dir.listFiles().orEmpty().count { isImage(it) }} 张全部可解码")

        // 清理
        val rm = gateway.httpDelete(ep.port, "/api/manga/library/$source/$comicId?files=1")
        assertTrue("清理应 200：HTTP ${rm.code}", rm.ok)
        evidenceFile().delete()
        gateway.stopEngine()
        ev("阶段 2 通过并清理完成")
    }

    private suspend fun taskStatus(gw: EngineGateway, port: Int, comicId: String): String =
        taskJson(gw, port, comicId).optString("status")

    private suspend fun taskImagesDone(gw: EngineGateway, port: Int, comicId: String): Int =
        taskJson(gw, port, comicId).optInt("images_done")

    private suspend fun taskImagesTotal(gw: EngineGateway, port: Int, comicId: String): Int =
        taskJson(gw, port, comicId).optInt("images_total")

    private suspend fun taskJson(gw: EngineGateway, port: Int, comicId: String): JSONObject {
        val r = gw.httpText(port, "/api/manga/download/status?source=$source&cid=$comicId")
        return runCatching { JSONObject(r.body) }.getOrDefault(JSONObject())
    }
}
