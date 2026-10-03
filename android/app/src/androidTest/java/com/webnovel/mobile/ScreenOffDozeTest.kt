package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.UUID

/**
 * **锁屏 / Doze** 韧性验收（方向基线 §7.2 P1-C："杀进程、锁屏、无网、磁盘满……"）。
 *
 * 口径（刻意不写"必须一直下载"这种做不到的承诺）：
 *   1. 熄屏后下载**不得产出坏文件**（逐张魔数 + 解码），任务状态不得谎报 done；
 *   2. 熄屏结束后（亮屏）数据仍完整，任务能继续或如实处于 paused/stopped/error；
 *   3. 进入 Doze 后，本机引擎不得被打死（健康接口仍可达）；下载若被系统限制，
 *      状态必须如实（running/paused/error），**不许装作完成**；
 *   4. 退出 Doze 后能继续（恢复 → 张数增长）。
 *
 * 屏幕/Doze 都用 shell 切（UiAutomation）；用例过程中不与界面交互，
 * 只经引擎接口与磁盘核对，因此熄屏不影响断言。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class ScreenOffDozeTest {

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext
    private var source = "mangadex"
    private var comicId = ""
    private var created = false
    private var deepSleepSupported = true

    private fun harnessRunId(): String =
        InstrumentationRegistry.getArguments().getString("dozeRunId").orEmpty()

    private fun harnessEnabled(): Boolean =
        InstrumentationRegistry.getArguments().getString("dozeHarness") == "1"

    private fun dozeEvidenceFile() = File(ctx.filesDir, "doze-check.json")

    private fun ev(line: String) = println("SCREEN_OFF_EVIDENCE $line")

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

    private fun chapterDir(chapterId: String) = File(OfflineStore.runtimeDir(ctx),
        "manga/downloads/$source/$comicId/$chapterId")

    private var chapterId = ""

    @After
    fun tearDown() {
        // The host-controlled phase 1 deliberately leaves the foreground service
        // and download alive after instrumentation exits. The shell harness owns
        // unforce + phase-2 recovery/cleanup; stopping the engine here would make
        // the Doze observation meaningless.
        val preserveForExternalDoze = harnessEnabled() &&
            InstrumentationRegistry.getArguments().getString("dozePhase") == "prepare"
        if (preserveForExternalDoze) return
        runCatching {
            // 无论成败都要亮屏、退出 Doze，别把设备留在特殊状态。
            // 只在"确实黑屏"时才按电源键——无条件按会把本来亮着的屏关掉。
            // KEYCODE_WAKEUP is idempotent; KEYCODE_POWER toggles and can leave
            // an already-awake emulator asleep during teardown.
            Shell.run("input keyevent 224")
            Shell.run("dumpsys deviceidle unforce")
            Shell.run("cmd power set-fixed-performance-mode-enabled false")
        }
        runCatching {
            runBlocking {
                val gateway = EngineGateway(ctx)
                var ep = gateway.currentEndpoint()
                if (ep == null) ep = (gateway.connect() as? EngineState.Ready)?.endpoint
                if (created && ep != null && comicId.isNotBlank()) {
                    gateway.httpPost(ep.port,
                        "/api/manga/download/pause?source=$source&cid=$comicId")
                    for (attempt in 0 until 30) {
                        val status = taskStatus(gateway, ep.port)
                        if (status !in listOf("running", "queued")) break
                        delay(500)
                    }
                    gateway.httpDelete(ep.port, "/api/manga/$source/$comicId/downloads")
                    gateway.httpDelete(ep.port, "/api/manga/favorites/$source/$comicId")
                    gateway.httpDelete(ep.port, "/api/manga/library/$source/$comicId")
                }
                gateway.stopEngine()
                if (comicId.startsWith("__screen_off_")) {
                    listOf("downloads", "_cache").forEach { root ->
                        File(OfflineStore.runtimeDir(ctx), "manga/$root/$source/$comicId")
                            .deleteRecursively()
                    }
                }
            }
        }
    }

    /** Recover only identities reserved for this suite if force-idle killed its runner. */
    @Test
    fun cleanupOrphanedSyntheticArtifacts(): Unit = runBlocking {
        val roots = listOf(
            File(OfflineStore.runtimeDir(ctx), "manga/downloads/$source"),
            File(OfflineStore.runtimeDir(ctx), "manga/_cache/$source"),
        )
        val ids = roots.flatMap { root -> root.listFiles().orEmpty()
            .filter { it.isDirectory && it.name.startsWith("__screen_off_") }
            .map { it.name } }.toSet()
        if (ids.isEmpty()) {
            dozeEvidenceFile().delete()
            return@runBlocking
        }
        val gateway = EngineGateway(ctx)
        val state = gateway.connect()
        assertTrue("隔离测试残留清理需要本机引擎：$state", state is EngineState.Ready)
        val ep = (state as EngineState.Ready).endpoint
        for (id in ids) {
            gateway.httpPost(ep.port, "/api/manga/download/pause?source=$source&cid=$id")
            val downloads = gateway.httpDelete(ep.port, "/api/manga/$source/$id/downloads")
            assertTrue("隔离下载/缓存清理失败：$id HTTP ${downloads.code}", downloads.ok)
            val favorite = gateway.httpDelete(ep.port, "/api/manga/favorites/$source/$id")
            assertTrue("隔离收藏清理失败：$id HTTP ${favorite.code}", favorite.ok)
            val shelf = gateway.httpDelete(ep.port, "/api/manga/library/$source/$id")
            assertTrue("隔离书架清理失败：$id HTTP ${shelf.code}", shelf.ok)
        }
        gateway.stopEngine()
        for (id in ids) {
            roots.forEach { File(it, id).deleteRecursively() }
            assertTrue("隔离测试目录未能删除：$id", roots.none { File(it, id).exists() })
        }
        dozeEvidenceFile().delete()
    }

    /** Phase one: prepare a real-source download, then leave the service running. */
    @Test
    fun prepareForExternalDoze(): Unit = runBlocking {
        assumeTrue("必须由 tools/android_doze_resilience.sh 驱动", harnessEnabled())
        assumeTrue("阶段标记必须为 prepare",
            InstrumentationRegistry.getArguments().getString("dozePhase") == "prepare")
        val runId = harnessRunId()
        assertTrue("Doze 演练必须使用唯一 run id", runId.isNotBlank())

        val gateway = EngineGateway(ctx)
        val state = gateway.connect()
        assertTrue("本机引擎未就绪：$state", state is EngineState.Ready)
        val ep = (state as EngineState.Ready).endpoint
        val tasks = gateway.httpText(ep.port, "/api/tasks")
        assertTrue("读取任务列表失败：HTTP ${tasks.code}", tasks.ok)
        val activeManga = EngineData.tasks(tasks.body).filter {
            it.isManga && it.status in listOf("running", "queued")
        }
        assertTrue("为隔离本轮下载，不允许存在其它活动漫画任务：$activeManga",
            activeManga.isEmpty())

        prepareTask(gateway, ep.port)
        val dir = chapterDir(chapterId)
        var count = 0
        for (i in 0 until 90) {
            delay(2000)
            count = dir.listFiles().orEmpty().count { isImage(it) }
            if (count >= 2) break
            if (taskStatus(gateway, ep.port) == "error") {
                assumeTrue("MangaDex 源不可用，本轮无法建立 Doze 下载样本", false)
            }
        }
        assertTrue("进入 Doze 前应有至少两张有效图片，实际 $count", count >= 2)
        val evidence = JSONObject()
            .put("run_id", runId).put("source", source).put("comic_id", comicId)
            .put("chapter_id", chapterId).put("before_count", count)
        check(!dozeEvidenceFile().exists()) { "存在未清理的 Doze 演练标记" }
        dozeEvidenceFile().writeText(evidence.toString(), Charsets.UTF_8)
        ev("EXTERNAL_DOZE_READY run=$runId comic=$comicId chapter=$chapterId images=$count")
        InstrumentationRegistry.getInstrumentation().sendStatus(2,
            android.os.Bundle().apply {
                putString("dozeReady", "READY")
                putString("dozeRunId", runId)
                putInt("dozeImageCount", count)
            })
    }

    /** Phase two: after the host has unforced Doze, validate and resume. */
    @Test
    fun verifyAfterExternalDozeAndResume(): Unit = runBlocking {
        assumeTrue("必须由 tools/android_doze_resilience.sh 驱动", harnessEnabled())
        assumeTrue("阶段标记必须为 verify",
            InstrumentationRegistry.getArguments().getString("dozePhase") == "verify")
        val evidenceFile = dozeEvidenceFile()
        assertTrue("阶段 1 的可恢复证据文件不存在", evidenceFile.isFile)
        val evidence = JSONObject(evidenceFile.readText(Charsets.UTF_8))
        val runId = harnessRunId()
        assertEquals("不能复用其它演练的证据", runId, evidence.optString("run_id"))
        source = evidence.getString("source")
        comicId = evidence.getString("comic_id")
        chapterId = evidence.getString("chapter_id")
        assertTrue("仅允许清理本测试命名空间", comicId.startsWith("__screen_off_"))
        created = true

        val gateway = EngineGateway(ctx)
        val state = gateway.connect()
        assertTrue("退出 Doze 后引擎应可启动/连接：$state", state is EngineState.Ready)
        val ep = (state as EngineState.Ready).endpoint
        val dir = chapterDir(chapterId)
        val beforeCount = evidence.optInt("before_count")
        val initialFiles = dir.listFiles().orEmpty().filter { isImage(it) }
        assertTrue("Doze 前的已完成图片必须保留", initialFiles.size >= beforeCount)
        for (file in initialFiles) {
            assertTrue("Doze 前后不得留下损坏图片：${file.name}", decodable(file))
        }

        val statusResponse = gateway.httpText(ep.port,
            "/api/manga/download/status?source=$source&cid=$comicId")
        assertTrue("恢复后的任务状态请求失败：HTTP ${statusResponse.code}",
            statusResponse.ok)
        val status = JSONObject(statusResponse.body).optString("status")
        assertTrue("任务状态必须诚实，不能丢失或伪报：$status",
            status in listOf("running", "queued", "paused", "stopped", "done", "error"))
        if (status != "done") {
            val resume = gateway.httpPost(ep.port,
                "/api/manga/download/resume?source=$source&cid=$comicId")
            assertTrue("Doze 后任务应可请求恢复：HTTP ${resume.code}", resume.ok)
        }

        var grewOrDone = status == "done"
        for (i in 0 until 60) {
            delay(2000)
            val count = dir.listFiles().orEmpty().count { isImage(it) }
            val current = taskStatus(gateway, ep.port)
            if (count > initialFiles.size || current == "done") {
                grewOrDone = true
                break
            }
            if (current == "error") break
        }
        val finalStatus = taskStatus(gateway, ep.port)
        val finalFiles = dir.listFiles().orEmpty().filter { isImage(it) }
        for (file in finalFiles) {
            assertTrue("恢复后不得留下损坏图片：${file.name}", decodable(file))
        }
        assertTrue("Doze 解除后下载应继续增长或如实完成（status=$finalStatus，" +
            "${finalFiles.size}/${initialFiles.size} 页）", grewOrDone)
        val health = gateway.httpText(ep.port, "/__mobile/health")
        assertTrue("恢复后本机引擎健康检查失败：HTTP ${health.code}", health.ok)
        ev("EXTERNAL_DOZE_RECOVERED run=$runId status=$finalStatus " +
            "images=${finalFiles.size} before=${initialFiles.size}")
        InstrumentationRegistry.getInstrumentation().sendStatus(2,
            android.os.Bundle().apply {
                putString("dozeRecovered", "PASS")
                putString("dozeRunId", runId)
                putString("dozeFinalStatus", finalStatus)
                putInt("dozeFinalImageCount", finalFiles.size)
                putInt("dozeInitialImageCount", initialFiles.size)
            })
        check(evidenceFile.delete()) { "无法清理本轮 Doze 证据标记" }
    }

    @Test
    fun screenOff_downloadKeepsGoingOrStaysHonest_withoutCorruptFiles(): Unit = runBlocking {
        val gateway = EngineGateway(ctx)
        val st = gateway.connect()
        assertTrue("引擎未就绪：$st", st is EngineState.Ready)
        val ep = (st as EngineState.Ready).endpoint
        prepareTask(gateway, ep.port)

        val dir = chapterDir(chapterId)
        var n0 = 0
        for (i in 0 until 45) {
            delay(2000)
            n0 = dir.listFiles().orEmpty().count { isImage(it) }
            if (n0 >= 2) break
            if (taskStatus(gateway, ep.port) == "error") {
                assumeTrue("漫画源不可用，无法验证熄屏下载：$comicId", false)
            }
        }
        assertTrue("熄屏前应已有图片落盘（实际 $n0）", n0 >= 2)
        ev("熄屏前：$n0 张")

        // 熄屏（等价于用户按电源键锁屏）
        Shell.run("input keyevent 26")
        delay(2000)
        val screenOff = Shell.run("dumpsys power | grep -m1 'mWakefulness='").trim()
        ev("已熄屏：$screenOff")
        delay(60_000)                     // 熄屏 60s 观察
        Shell.run("input keyevent 26")    // 亮屏
        delay(3000)

        val files = dir.listFiles().orEmpty().filter { isImage(it) }
        val status = taskStatus(gateway, ep.port)
        ev("熄屏 60s 后：status=$status，张数 ${files.size}（熄屏前 $n0）")

        // 1) 不得产出坏文件：逐张魔数 + 解码
        for (f in files) {
            assertTrue("熄屏期间不得写出坏文件：${f.name}（${f.length()} 字节）", decodable(f))
        }
        ev("熄屏期间 ${files.size} 张全部可解码（无坏文件）")

        // 2) 状态必须诚实：done 要求真的下完（张数不再增长也算允许），否则必须说明未完成
        assertTrue("任务状态必须明确（实际「$status」）",
            status in listOf("running", "queued", "paused", "stopped", "done", "error"))
        if (status == "done") {
            assertTrue("标记 done 时磁盘上必须有图片", files.isNotEmpty())
        }
        ev("熄屏后状态诚实：$status")

        // 3) 引擎仍在本机可用（前台服务没被系统顺手杀掉）
        val h = gateway.httpText(ep.port, "/__mobile/health")
        assertTrue("熄屏后本机引擎应仍可达（HTTP ${h.code}）", h.ok)
        ev("熄屏后引擎仍可达")
    }

    @Test
    fun doze_engineStaysReachable_andResumes(): Unit = runBlocking {
        assumeTrue("Doze 必须用外部两阶段 harness，防止仪器进程一同被系统回收",
            harnessEnabled() && InstrumentationRegistry.getArguments()
                .getString("dozePhase") == "legacy")
        val gateway = EngineGateway(ctx)
        val st = gateway.connect()
        assertTrue("引擎未就绪：$st", st is EngineState.Ready)
        val ep = (st as EngineState.Ready).endpoint
        prepareTask(gateway, ep.port)

        val dir = chapterDir(chapterId)
        var n0 = 0
        for (i in 0 until 45) {
            delay(2000)
            n0 = dir.listFiles().orEmpty().count { isImage(it) }
            if (n0 >= 2) break
        }
        assertTrue("进入 Doze 前应已有图片落盘（实际 $n0）", n0 >= 2)

        Shell.run("dumpsys deviceidle force-idle")
        delay(5000)
        val idle = Shell.run("dumpsys deviceidle get deep").trim()
        ev("已请求进入 Doze：$idle（空/unknown 表示系统不支持，如实记录不假装验证过）")
        if (idle.isBlank() || idle.equals("unknown", true)) deepSleepSupported = false

        delay(40_000)                     // Doze 中观察
        val midStatus = taskStatus(gateway, ep.port)
        val midFiles = dir.listFiles().orEmpty().filter { isImage(it) }
        ev("Doze 中：status=$midStatus，张数 ${midFiles.size}（进入前 $n0）")
        // 1) 引擎不得被打死（前台服务）——健康接口必须仍可达
        val h = gateway.httpText(ep.port, "/__mobile/health")
        assertTrue("Doze 中本机引擎应仍可达（HTTP ${h.code}）", h.ok)
        // 2) 状态诚实 + 无坏文件
        assertTrue("Doze 中状态必须明确（实际「$midStatus」）",
            midStatus in listOf("running", "queued", "paused", "stopped", "done", "error"))
        for (f in midFiles) {
            assertTrue("Doze 中不得写出坏文件：${f.name}", decodable(f))
        }
        ev("Doze 中：引擎可达、状态诚实、${midFiles.size} 张全部可解码")

        // 3) 退出 Doze → 应能继续（显式恢复；若已 done 则至少张数不少）
        Shell.run("dumpsys deviceidle unforce")
        delay(3000)
        gateway.httpPost(ep.port, "/api/manga/download/resume?source=$source&cid=$comicId")
        var grew = false
        for (i in 0 until 30) {
            delay(2000)
            if (dir.listFiles().orEmpty().count { isImage(it) } > midFiles.size) {
                grew = true; break
            }
        }
        val finalStatus = taskStatus(gateway, ep.port)
        val nEnd = dir.listFiles().orEmpty().count { isImage(it) }
        ev("退出 Doze 后：status=$finalStatus，张数 $nEnd（Doze 中 ${midFiles.size}）；" +
            "deepSleepSupported=$deepSleepSupported")
        assertTrue("退出 Doze 后应能继续下载（或已完成）",
            grew || finalStatus == "done" || nEnd > midFiles.size)
        for (f in dir.listFiles().orEmpty().filter { isImage(it) }) {
            assertTrue("恢复后不得出现坏文件：${f.name}", decodable(f))
        }
    }

    private suspend fun prepareTask(gw: EngineGateway, port: Int) {
        // 夹具解析：优先缓存、失败才搜索（源站限流不再让本用例成片失败）
        val fx = MangaFixture.pick(gw, port, source) { ev(it) }
        source = fx.source
        assumeTrue("熄屏下载真实源验证当前仅支持 MangaDex", source == "mangadex")
        chapterId = fx.chapterId
        val realComicId = fx.comicId
        comicId = "__screen_off_${UUID.randomUUID()}__"
        val detailResponse = gw.httpText(port, "/api/manga/$source/$realComicId")
        assertTrue("真实作品详情应可读取：HTTP ${detailResponse.code}", detailResponse.ok)
        val detail = EngineData.mangaDetail(detailResponse.body)
            ?: throw AssertionError("真实作品详情解析失败")
        val chapter = detail.chapters.firstOrNull { it.id == chapterId }
            ?: throw AssertionError("夹具章节不在真实作品目录中")
        val localDir = File(OfflineStore.runtimeDir(ctx), "manga/downloads/$source/$comicId")
        check(!localDir.exists()) { "测试随机目录意外已存在：$localDir" }
        check(localDir.mkdirs()) { "无法创建隔离测试目录：$localDir" }
        val info = JSONObject()
            .put("title", detail.title)
            .put("cover", detail.cover)
            .put("chapters", JSONArray().put(JSONObject()
                .put("id", chapter.id)
                .put("name", chapter.name)
                .put("group", chapter.group)))
        File(localDir, "_info.json").writeText(info.toString())
        val body = JSONObject().put("title", fx.title).put("cover", fx.cover)
            .put("chapters", JSONArray().put(chapterId)).toString()
        val ok = gw.httpPost(port, "/api/manga/$source/$comicId/download", body)
        assertTrue("创建下载任务应 2xx：HTTP ${ok.code}", ok.code in 200..299)
        created = true
        ev("隔离任务已创建：$comicId / $chapterId（真实源作品 $realComicId）")
    }

    private suspend fun taskStatus(gw: EngineGateway, port: Int): String {
        val r = gw.httpText(port, "/api/manga/download/status?source=$source&cid=$comicId")
        return runCatching { JSONObject(r.body).optString("status") }.getOrDefault("")
    }
}
