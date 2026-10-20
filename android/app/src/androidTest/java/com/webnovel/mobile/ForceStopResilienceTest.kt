package com.webnovel.mobile

import android.util.AtomicFile
import android.system.Os
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.FixMethodOrder
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.MethodSorters
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.UUID

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
    private var source = "mangadex"

    private fun ev(line: String) = println("FORCESTOP_EVIDENCE $line")

    private fun evidenceFile() = File(ctx.filesDir, "forcestop-check.json")

    private fun queuedEvidenceFile() = File(
        ctx.filesDir, "forcestop-queued-${harnessRunId()}.json")

    /** Remove only this run's key from the recoverable task snapshot as well as the primary. */
    private fun removeQueuedIdentityFromBackup(key: String) {
        val file = File(OfflineStore.runtimeDir(ctx), "manga/_tasks.json.bak")
        if (!file.isFile) return
        val snapshot = JSONObject(file.readText(Charsets.UTF_8))
        if (!snapshot.has(key)) return
        snapshot.remove(key)
        if (snapshot.length() == 0) {
            check(file.delete()) { "无法删除仅包含本轮测试任务的备份快照" }
            return
        }
        val temp = File(file.parentFile, ".${file.name}.${UUID.randomUUID()}.tmp")
        try {
            FileOutputStream(temp).use { stream ->
                stream.write(snapshot.toString().toByteArray(Charsets.UTF_8))
                stream.fd.sync()
            }
            // Same-directory rename is atomic and preserves unrelated queue identities.
            Os.rename(temp.absolutePath, file.absolutePath)
        } finally {
            temp.delete()
        }
    }

    private fun harnessRunId(): String =
        InstrumentationRegistry.getArguments().getString("forceStopRunId").orEmpty()

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

    private fun downloadedImages(comicId: String, chapterIds: List<String>): List<Pair<String, File>> =
        chapterIds.flatMap { id ->
            chapterDir(comicId, id).listFiles().orEmpty()
                .filter { isImage(it) }
                .map { id to it }
        }

    // ── 专项：持久化队列项经真实进程强杀后必须可继续 ──────────────────

    @Test
    fun phase1_writeQueuedTaskAndWaitForForceStop(): Unit = runBlocking {
        assumeTrue("该阶段必须由外部 force-stop harness 驱动",
            InstrumentationRegistry.getArguments().getString("forceStopHarness") == "1")
        val runId = harnessRunId()
        assertTrue("queued 强杀测试必须使用本轮唯一 run id", runId.isNotBlank())
        val gateway = EngineGateway(ctx)
        val state = gateway.connect()
        assertTrue("引擎未就绪：$state", state is EngineState.Ready)
        val ep = (state as EngineState.Ready).endpoint
        val existing = gateway.httpText(ep.port, "/api/tasks")
        assertTrue("读取任务列表应成功：HTTP ${existing.code}", existing.ok)
        val active = EngineData.tasks(existing.body).filter {
            it.isManga && it.status in listOf("running", "queued")
        }
        assertTrue("为避免后台保存覆盖隔离快照，AVD 不应有其他活动漫画任务：$active",
            active.isEmpty())

        source = "mangadex"
        val comicId = UUID.randomUUID().toString()
        val key = "$source:$comicId"
        val taskFile = File(OfflineStore.runtimeDir(ctx), "manga/_tasks.json")
        taskFile.parentFile?.mkdirs()
        // 只在现有合法快照上增加唯一测试条目；损坏/非对象快照一律拒绝覆盖。
        val snapshot = if (taskFile.isFile) {
            JSONObject(taskFile.readText(Charsets.UTF_8))
        } else JSONObject()
        assertTrue("随机测试身份不应与任何现有任务冲突", !snapshot.has(key))
        val queued = JSONObject()
            .put("status", "queued").put("total", 1).put("done", 0)
            .put("current", "等待同源下载任务").put("error", "")
            .put("title", "强杀恢复排队测试").put("source", source)
            .put("comic_id", comicId).put("cover", "")
            .put("speed", 0).put("eta", 0).put("images_done", 0)
            .put("images_total", 0).put("failed_chapters", 0)
            .put("started_at", System.currentTimeMillis() / 1000.0)
            .put("type", "manga").put("chapters", JSONArray().put("queued-fixture"))
            .put("paused", false).put("stop_kind", "").put("stop_reason", "")
        snapshot.put(key, queued)
        val atomic = AtomicFile(taskFile)
        val stream = atomic.startWrite()
        try {
            stream.write(snapshot.toString().toByteArray(Charsets.UTF_8))
            stream.fd.sync()
            atomic.finishWrite(stream)
        } catch (t: Throwable) {
            atomic.failWrite(stream)
            throw t
        }
        queuedEvidenceFile().writeText(
            JSONObject().put("run_id", runId).put("source", source)
                .put("comic_id", comicId).toString(), Charsets.UTF_8)
        ev("已原子写入唯一 queued 快照：$key")
        InstrumentationRegistry.getInstrumentation().sendStatus(2,
            android.os.Bundle().apply {
                putString("forceStopState", "READY")
                putString("forceStopRunId", runId)
                putString("forceStopComicId", comicId)
            })
        val started = System.currentTimeMillis()
        while (System.currentTimeMillis() - started < 90_000L) delay(1000)
        ev("等待结束（外部未强杀则本轮没有验证到重启）")
    }

    @Test
    fun phase2_verifyQueuedTaskRecoveredAndCleanFixture(): Unit = runBlocking {
        val evidence = queuedEvidenceFile()
        assumeTrue("阶段 1 未生成排队任务证据", evidence.isFile)
        val e = JSONObject(evidence.readText(Charsets.UTF_8))
        val expectedRunId = harnessRunId()
        assertEquals("不得复用上一轮 queued 快照", expectedRunId, e.optString("run_id"))
        source = e.getString("source")
        val comicId = e.getString("comic_id")
        val gateway = EngineGateway(ctx)
        val connected = gateway.connect()
        assertTrue("强杀后引擎应能重新启动：$connected", connected is EngineState.Ready)
        val ep = (connected as EngineState.Ready).endpoint
        try {
            val response = gateway.httpText(ep.port,
                "/api/manga/download/status?source=$source&cid=$comicId")
            assertTrue("恢复任务状态应成功：HTTP ${response.code}", response.ok)
            val task = JSONObject(response.body)
            assertEquals("孤立排队项必须收敛为可继续状态", "stopped",
                task.optString("status"))
            assertEquals("process_restart", task.optString("stop_kind"))
            assertTrue(task.optString("stop_reason").contains("仍在等待队列"))
            assertTrue(task.optString("stop_reason").contains("尚未开始执行"))
            assertEquals("", task.optString("current"))

            val allResponse = gateway.httpText(ep.port, "/api/tasks")
            assertTrue("下载中心任务列表应成功：HTTP ${allResponse.code}", allResponse.ok)
            val parsed = EngineData.tasks(allResponse.body).firstOrNull {
                it.mangaKey == "$source:$comicId"
            } ?: throw AssertionError("恢复后的排队任务应仍出现在下载中心")
            assertTrue("恢复后的任务应提供继续入口", parsed.resumable)
            assertTrue("恢复原因应展示给用户", parsed.showStopReason)
            assertTrue(parsed.stopReason.contains("等待队列"))
            ev("API35 强杀后 queued → stopped，可继续且原因可见")
        } finally {
            // 仅删除本轮 UUID 的任务记录；从不清理整个任务文件或其他用户记录。
            val cleanup = gateway.httpDelete(ep.port,
                "/api/tasks/manga_${source}:$comicId")
            assertTrue("应能删除本轮唯一测试任务：HTTP ${cleanup.code}", cleanup.ok)
            assertTrue("停止引擎后应能检查测试备份清理",
                gateway.stopEngine())
            removeQueuedIdentityFromBackup("$source:$comicId")
            evidence.delete()
        }
    }

    // ── 阶段 1：起一个真实下载，把现场记录下来交给外部强杀 ──────────────

    @Test
    fun phase1_startDownloadAndRecord(): Unit = runBlocking {
        val gateway = EngineGateway(ctx)
        val st = gateway.connect()
        assertTrue("引擎未就绪：$st", st is EngineState.Ready)
        val ep = (st as EngineState.Ready).endpoint

        // 夹具解析：优先缓存、失败才搜索（源站限流不再让本用例成片失败）
        val fx = MangaFixture.pick(gateway, ep.port, source) { ev(it) }
        // MangaDex 的图片列表按 chapter id 获取，与 manga id 无关。复用真实章节 ID
        // 但将下载内容、任务、书库与自动收藏全部写入本轮随机 comic id，避免测试
        // 在清理时触及该真实作品或任何已有本地下载。
        assumeTrue("中途下载强杀用例要求 MangaDex 章节 API", fx.source == "mangadex")
        source = fx.source
        val realComicId = fx.comicId
        val comicId = "__force_stop_${UUID.randomUUID().toString().replace("-", "") }__"
        val chapterId = fx.chapterId
        val detailResp = gateway.httpText(ep.port, "/api/manga/$source/$realComicId")
        assertTrue("读取下载目录应成功：HTTP ${detailResp.code}", detailResp.ok)
        val detail = EngineData.mangaDetail(detailResp.body)
            ?: throw AssertionError("下载目录无法解析")
        // 下载多个单元，避免单话很短时整个任务在强杀触发前已完成；目标章节仍包含夹具话。
        val availableChapterIds = detail.readingChapters.map { it.id }.filter { it.isNotBlank() }
        val fixtureIndex = availableChapterIds.indexOf(chapterId)
        val chapterIds = if (fixtureIndex >= 0) {
            availableChapterIds.subList((fixtureIndex - 7).coerceAtLeast(0), fixtureIndex + 1)
        } else listOf(chapterId)
        assertTrue("强杀任务必须包含夹具章节", chapterId in chapterIds)

        // 下载器从独立 downloads 根加载这份刚创建的目录快照；MangaDex.images()
        // 只使用章节 ID。随机身份目录必须原先不存在，且只由本用例写入/删除。
        val syntheticDir = File(OfflineStore.runtimeDir(ctx),
            "manga/downloads/$source/$comicId")
        assertTrue("本轮随机下载目录不应预先存在", !syntheticDir.exists())
        assertTrue("创建本轮随机下载目录失败", syntheticDir.mkdirs())
        val chapterRows = detail.readingChapters.filter { it.id in chapterIds }
        assertTrue("随机作品清单必须包含下载章节", chapterRows.size == chapterIds.size)
        File(syntheticDir, "_info.json").writeText(
            JSONObject().put("title", fx.title).put("cover", fx.cover)
                .put("chapters", JSONArray().apply {
                    chapterRows.forEach { ch ->
                        put(JSONObject().put("id", ch.id).put("name", ch.name)
                            .put("group", ch.group))
                    }
                }).toString(), Charsets.UTF_8)
        val body = JSONObject().put("title", fx.title).put("cover", fx.cover)
            .put("chapters", JSONArray().apply { chapterIds.forEach { put(it) } }).toString()
        val ok = gateway.httpPost(ep.port, "/api/manga/$source/$comicId/download", body)
        if (ok.code !in 200..299) {
            syntheticDir.deleteRecursively()
            throw AssertionError("创建随机隔离下载任务失败：HTTP ${ok.code} ${ok.body}")
        }

        // 等几张真图落盘，但**不要**等它跑完：要的就是"下到一半被系统强杀"
        var n = 0
        for (i in 0 until 60) {
            delay(1500)
            n = downloadedImages(comicId, chapterIds).size
            if (n >= 3) break
            val state = taskStatus(gateway, ep.port, comicId)
            if (state == "error") {
                val error = taskJson(gateway, ep.port, comicId).optString("error")
                ev("随机隔离下载失败：$error")
                cleanupSyntheticDownload(gateway, ep.port, comicId)
                assumeTrue("漫画源不可用，无法执行强杀下载断言：" +
                    "任务进入 error 状态（$error）", false)
            }
        }
        // ≥1 张即可：这里只要求"磁盘上确实有东西"，具体张数由阶段 2 与外部强杀共同决定
        if (n < 1) cleanupSyntheticDownload(gateway, ep.port, comicId)
        assertTrue("强杀前应已有图片落盘（实际 $n）", n >= 1)
        val status = taskStatus(gateway, ep.port, comicId)
        val files = downloadedImages(comicId, chapterIds)
        val evJson = JSONObject()
            .put("created_at", System.currentTimeMillis())
            .put("run_id", harnessRunId())
            .put("source", source).put("comic_id", comicId).put("chapter_id", chapterId)
            .put("chapter_ids", JSONArray().apply { chapterIds.forEach { put(it) } })
            .put("title", fx.title).put("chapter_label", fx.chapterId)
            .put("images", files.size).put("status", status)
            .put("hashes", JSONObject().apply {
                chapterIds.forEach { id ->
                    put(id, JSONObject().apply {
                        files.filter { it.first == id }.forEach { (_, file) -> put(file.name, sha(file)) }
                    })
                }
            })
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
            // READY 必须代表现场记录后又确实推进过下载，否则立即 force-stop 会和
            // 下一张图片落盘竞态，把慢网误判成强杀缺陷。
            val progressDeadline = System.currentTimeMillis() + 45_000L
            var afterRecord = downloadedImages(comicId, chapterIds).size
            while (afterRecord <= files.size && System.currentTimeMillis() < progressDeadline &&
                taskStatus(gateway, ep.port, comicId) !in listOf("done", "error")) {
                delay(500)
                afterRecord = downloadedImages(comicId, chapterIds).size
            }
            assertTrue("强杀 READY 前必须观察到记录后的新增图片（记录 ${files.size}，当前 $afterRecord）",
                afterRecord > files.size)
            assertTrue("强杀 READY 前任务仍须未完成",
                taskStatus(gateway, ep.port, comicId) != "done")
            ev("记录后下载继续推进：${files.size} → $afterRecord 张")
            ev("READY_TO_BE_FORCE_STOPPED（外部执行 adb shell am force-stop com.webnovel.mobile）")
            InstrumentationRegistry.getInstrumentation().sendStatus(2, android.os.Bundle().apply {
                putString("forceStopState", "READY")
                putString("forceStopRunId", harnessRunId())
                putString("forceStopComicId", comicId)
            })
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
        assumeTrue("阶段 1 未生成强杀证据（可能因目标源不可用而跳过）",
            evidenceFile().exists())
        val e = JSONObject(evidenceFile().readText(Charsets.UTF_8))
        source = e.getString("source")
        val comicId = e.getString("comic_id")
        val chapterId = e.getString("chapter_id")
        val chapterIds = e.optJSONArray("chapter_ids")?.let { array ->
            (0 until array.length()).map { array.getString(it) }
        }?.filter { it.isNotBlank() }.orEmpty().ifEmpty { listOf(chapterId) }
        val before = e.getJSONObject("hashes")
        val harness = InstrumentationRegistry.getArguments().getString("forceStopHarness") == "1"
        if (harness) {
            val expectedRunId = harnessRunId()
            assertTrue("强杀阶段必须传入本次 run id", expectedRunId.isNotBlank())
            assertEquals("不得使用历史强杀证据", expectedRunId,
                e.optString("run_id"))
        }

        // 1) 强杀之后、**重新拉起之前**：磁盘上的东西必须仍然干净
        val leftovers = chapterIds.flatMap { id ->
            chapterDir(comicId, id).listFiles().orEmpty().filter { it.name.endsWith(".tmp") }
        }
        assertEquals("强杀不得留下 .tmp 残渣（实际 ${leftovers.map { it.name }}）",
            0, leftovers.size)
        val now = downloadedImages(comicId, chapterIds)
        for (id in chapterIds) {
            val chapterHashes = before.optJSONObject(id) ?: JSONObject()
            for (name in chapterHashes.keys()) {
                val f = File(chapterDir(comicId, id), name)
                assertTrue("强杀前已落盘的图片必须还在：$id/$name", f.isFile)
                assertEquals("强杀不得改动已落盘的图片：$id/$name",
                    chapterHashes.getString(name), sha(f))
            }
        }
        for ((id, f) in now) {
            assertTrue("强杀后每张图都必须能解码：$id/${f.name}（${f.length()} 字节）",
                decodable(f))
        }
        ev("强杀后磁盘：${now.size} 张图片全部完整可解码，无 .tmp 残渣")

        // 2) 重新拉起（等价于用户重新点图标）→ 引擎必须能起来
        val gateway = EngineGateway(ctx)
        val st = gateway.connect()
        assertTrue("强杀后应能重新拉起引擎：$st", st is EngineState.Ready)
        val ep = (st as EngineState.Ready).endpoint
        try {
        ev("强杀后重新拉起：引擎就绪 port=${ep.port}")

        // 3) 任务状态必须诚实：不许谎报 done（它没下完）
        val status = taskStatus(gateway, ep.port, comicId)
        val total = taskImagesTotal(gateway, ep.port, comicId)
        val done = taskImagesDone(gateway, ep.port, comicId)
        ev("强杀后任务：status=$status images_done=$done images_total=$total")
        assertTrue("强杀后任务状态必须明确（实际「$status」）",
            status in listOf("running", "queued", "paused", "stopped", "idle", "error", "done"))
        if (harness) {
            if (total > 0 && now.size < total) {
                assertTrue("强杀前未完成就不许标 done（本地 ${now.size} / 共 $total 张）",
                    status != "done")
            }
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
            if (downloadedImages(comicId, chapterIds).size > now.size) { grew = true; break }
            status2 = taskStatus(gateway, ep.port, comicId)
            if (status2 == "error" && i > 4) break
            if (status2 == "done") break
        }
        val nEnd = downloadedImages(comicId, chapterIds).size
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
        for ((id, f) in downloadedImages(comicId, chapterIds)) {
            assertTrue("续传后每张图都必须能解码：$id/${f.name}", decodable(f))
        }
        ev("续传后 ${downloadedImages(comicId, chapterIds).size} 张全部可解码")

        ev("阶段 2 验证通过")
        } finally {
            runCatching { cleanupSyntheticDownload(gateway, ep.port, comicId) }
                .onFailure { ev("本轮 UUID 隔离下载清理失败：${it.message}") }
            evidenceFile().delete()
            gateway.stopEngine()
            File(OfflineStore.runtimeDir(ctx), "manga/_cache/$source/$comicId")
                .deleteRecursively()
        }
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

    /** Stop and remove only the UUID-scoped fixture, including download auto-favorite. */
    private suspend fun cleanupSyntheticDownload(gateway: EngineGateway, port: Int,
                                                 comicId: String) {
        gateway.httpPost(port,
            "/api/manga/download/pause?source=$source&cid=$comicId")
        var status = taskStatus(gateway, port, comicId)
        for (attempt in 0 until 30) {
            if (status !in listOf("running", "queued")) break
            delay(500)
            status = taskStatus(gateway, port, comicId)
        }
        check(status !in listOf("running", "queued")) {
            "隔离下载 worker 未停止，拒绝删除测试内容（status=$status）"
        }
        val media = gateway.httpDelete(port, "/api/manga/$source/$comicId/downloads")
        check(media.ok) { "隔离下载内容清理失败：HTTP ${media.code} ${media.body}" }
        val favorite = gateway.httpDelete(port, "/api/manga/favorites/$source/$comicId")
        check(favorite.ok) { "隔离自动收藏清理失败：HTTP ${favorite.code} ${favorite.body}" }
        val shelf = gateway.httpDelete(port, "/api/manga/library/$source/$comicId")
        check(shelf.ok) { "隔离书架条目清理失败：HTTP ${shelf.code} ${shelf.body}" }
    }

}
