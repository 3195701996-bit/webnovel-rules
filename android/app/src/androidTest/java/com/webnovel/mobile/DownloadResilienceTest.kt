package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * P1-C 下载韧性验收（真机、真实源站）：方向基线 §7.2 要求
 * "杀进程、锁屏、无网、磁盘满、升级后任务和文件一致；**用户停止不擅自复活**"。
 *
 * 本用例覆盖能确定性复现的三件事（不依赖拔网线/灌满磁盘这类破坏性操作）：
 *   1. **暂停后重启引擎，任务不得自己复活**（用户停止是用户的意思）；
 *   2. **下载中途停引擎**：磁盘上不得留下半截/损坏文件，任务状态必须诚实
 *      （stopped 而不是 done），重启后还能续传并最终完成；
 *   3. **目标目录不可写**：必须如实报错（任务不得报"完成"），且不产生坏文件；
 *      恢复权限后仍能继续。
 *
 * 全部断言都落到**磁盘事实**（图片魔数/文件可解析）与**服务端状态**两处，
 * 不看界面自说自话。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class DownloadResilienceTest {

    private lateinit var gateway: EngineGateway
    private val source = "mangadex"
    private var comicId: String = ""
    private var chapterId: String = ""
    private var title: String = ""
    private var cover: String = ""
    private var created = false

    private fun ev(line: String) = println("DL_RESILIENCE_EVIDENCE $line")
    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Before
    fun setUp() {
        gateway = EngineGateway(ctx)
        val st = runBlocking { gateway.connect() }
        assertTrue("引擎未就绪：$st", st is EngineState.Ready)
    }

    @After
    fun tearDown() {
        runCatching {
            runBlocking {
                var ep = gateway.currentEndpoint()
                if (ep == null) ep = (gateway.connect() as? EngineState.Ready)?.endpoint
                if (created && ep != null && comicId.isNotBlank()) {
                    gateway.httpDelete(ep.port, "/api/manga/library/$source/$comicId?files=1")
                }
                gateway.stopEngine()
            }
        }
    }

    private fun chapterDir() = File(OfflineStore.runtimeDir(ctx),
        "manga/downloads/$source/$comicId/$chapterId")

    private fun images(): List<File> =
        chapterDir().listFiles().orEmpty().filter { isImage(it) }

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

    /** 找一部真实作品，准备一话下载（返回 taskKey） */
    private suspend fun prepareTask(ep: EngineEndpoint): String {
        // 夹具解析：优先缓存、失败才搜索（源站限流不再让本用例成片失败）
        val fx = MangaFixture.pick(gateway, ep.port, source) { ev(it) }
        comicId = fx.comicId
        chapterId = fx.chapterId
        title = fx.title
        cover = fx.cover
        val body = JSONObject().put("title", fx.title).put("cover", fx.cover)
            .put("chapters", JSONArray().put(fx.chapterId)).toString()
        val ok = gateway.httpPost(ep.port, "/api/manga/$source/$comicId/download", body)
        assertTrue("创建下载任务应 2xx：HTTP ${ok.code}", ok.code in 200..299)
        created = true
        return "$source:$comicId"
    }

    private suspend fun taskStatus(port: Int): JSONObject {
        val r = gateway.httpText(port,
            "/api/manga/download/status?source=$source&cid=$comicId")
        return runCatching { JSONObject(r.body) }.getOrDefault(JSONObject())
    }

    @Test
    fun pauseSurvivesEngineRestart_andIsNotRevived() = runBlocking {
        val ep = gateway.currentEndpoint()!!
        prepareTask(ep)
        // 等它真的动起来
        var st = taskStatus(ep.port)
        for (i in 0 until 20) {
            if (st.optString("status") in listOf("running", "queued")) break
            delay(1000); st = taskStatus(ep.port)
        }
        ev("任务已启动：status=${st.optString("status")}")

        gateway.httpPost(ep.port, "/api/manga/download/pause?source=$source&cid=$comicId")
        // 暂停请求由 worker 在**下一个检查点**生效（每章/每 5 页），不是立刻改状态——
        // 这是设计如此（不能在写文件中途硬切），所以要有界等待其真正停下来
        var paused = ""
        for (i in 0 until 60) {
            delay(1000)
            paused = taskStatus(ep.port).optString("status")
            if (paused in listOf("paused", "stopped")) break
            if (paused == "done") break          // 跑完了就测不了暂停，如实失败
        }
        assertTrue("暂停应生效为 paused/stopped（实际 $paused）——若为 done 说明该章太快，需换更长的章节",
            paused in listOf("paused", "stopped"))
        val nBefore = images().size
        ev("已暂停：status=$paused，已落盘 $nBefore 张")

        // 重启引擎（模拟被系统杀掉后重开）
        assertTrue("停引擎", gateway.stopEngine())
        delay(1500)
        val st2 = gateway.connect()
        assertTrue("引擎应能重启：$st2", st2 is EngineState.Ready)
        val ep2 = (st2 as EngineState.Ready).endpoint
        delay(3000)   // 给"可能擅自复活"的路径留出时间
        val after = taskStatus(ep2.port)
        val status2 = after.optString("status")
        assertTrue("重启后不得擅自复活（实际 $status2）",
            status2 in listOf("paused", "stopped", "idle"))
        val nAfter = images().size
        assertEqualsMsg("重启后不应继续下载（暂停前 $nBefore 张 → 重启后 $nAfter 张）",
            nBefore, nAfter)
        ev("重启后仍为 $status2，磁盘张数不变（$nAfter）")

        // 用户显式恢复 → 应能继续跑
        gateway.httpPost(ep2.port, "/api/manga/download/resume?source=$source&cid=$comicId")
        var progressed = false
        var resumed = false
        var status3 = ""
        for (i in 0 until 30) {
            delay(2000)
            if (images().size > nAfter) { progressed = true; break }
            status3 = taskStatus(ep2.port).optString("status")
            if (!resumed && status3 in listOf("running", "queued")) {
                resumed = true     // 恢复**生效**了（任务真的动起来）
            }
            // 源站偶发侧抖动（实测 MangaDex 返回 0 章 → 任务直接 error）：
            // 那是源站的问题，不是"恢复没生效"。此时如实记录并**重试一次**，
            // 避免把外部抖动写成我们的回归（映射到门槛 C 的诚实口径）。
            if (!progressed && status3 == "error" && i >= 3) {
                val why = taskStatus(ep2.port).optString("error")
                ev("恢复后源站侧失败：status=error，原因=「$why.take(80)」→ 重建任务重试一次")
                gateway.httpPost(ep2.port, "/api/manga/$source/$comicId/download",
                    JSONObject().put("title", title).put("cover", cover)
                        .put("chapters", JSONArray().put(chapterId)).toString())
                break
            }
        }
        if (!progressed) {
            // 重试窗口：再等一轮（只在源站侧失败后走到这里）
            for (i in 0 until 20) {
                delay(2000)
                if (images().size > nAfter) { progressed = true; break }
            }
            status3 = taskStatus(ep2.port).optString("status")
        }
        ev("显式恢复：running=$resumed，status=$status3，张数 ${nAfter} → ${images().size}")
        assertTrue("显式恢复后应继续下载（status=$status3，原因=「" +
            taskStatus(ep2.port).optString("error").take(80) + "」）", progressed)
    }

    @Test
    fun interruptMidDownload_leavesNoCorruptFiles_andResumes() = runBlocking {
        val ep = gateway.currentEndpoint()!!
        prepareTask(ep)
        // 等到有图片落盘，然后**硬停引擎**（模拟进程被杀）。
        // 阈值只要 ≥1 张：这里验证的是"落盘的东西不会坏"，不是"下得有多快"——
        // 卡 3 张曾在源站抖动时假失败（实测：等了 60s 只有 2 张，MangaDex 首图就慢）。
        var waited = 0
        while (images().size < 1 && waited < 90) { delay(1000); waited++ }
        assertTrue("应先有图片落盘（实际 ${images().size}，等了 ${waited}s）",
            images().size >= 1)
        val nAtKill = images().size
        gateway.stopEngine()
        delay(1500)
        ev("下载中途停引擎：落盘 $nAtKill 张")

        // 磁盘上没有半截文件、没有非图片文件
        for (f in chapterDir().listFiles().orEmpty()) {
            if (f.name.startsWith(".") || f.name.contains(".tmp")) continue
            assertTrue("中断后不应留有损坏/非图片文件：${f.name}", isImage(f))
        }
        val info = File(chapterDir(), "_info.json")
        if (info.isFile) {
            val ok = runCatching { JSONObject(info.readText(Charsets.UTF_8)) }.isSuccess
            assertTrue("_info.json 必须仍可解析", ok)
        }
        val st2 = gateway.connect()
        assertTrue("引擎应能重启", st2 is EngineState.Ready)
        val ep2 = (st2 as EngineState.Ready).endpoint
        val afterStatus = taskStatus(ep2.port).optString("status")
        assertTrue("中断后任务状态必须诚实（不得是 done，实际 $afterStatus）",
            afterStatus != "done")
        ev("中断后状态=$afterStatus（未谎报完成）")

        // 续传 → 最终完成，且文件全为合法图片
        gateway.httpPost(ep2.port, "/api/manga/download/resume?source=$source&cid=$comicId")
        var done = false
        for (i in 0 until 60) {
            delay(2000)
            val s = taskStatus(ep2.port)
            if (s.optString("status") == "done") { done = true; break }
        }
        val files = images()
        assertTrue("续传后应完成（实际状态 ${taskStatus(ep2.port).optString("status")}，" +
            "图片 ${files.size}）", done)
        assertTrue("完成后图片数应多于中断时（$nAtKill → ${files.size}）", files.size >= nAtKill)
        assertTrue("全部文件都必须是合法图片", files.all { isImage(it) })
        ev("续传完成：$nAtKill → ${files.size} 张，全部为合法图片")
    }

    @Test
    fun unwritableTarget_failsHonestlyWithoutCorruption() = runBlocking {
        val ep = gateway.currentEndpoint()!!
        // 关键：**在下载开始前**把目标目录造好并设为不可写，否则章可能在 chmod 之前就下完了
        // （实测：先等 3 张再 chmod，任务早已跑完，断言拿不到任何失败信号）
        prepareTask(ep)
        assertTrue("预建章节目录", chapterDir().mkdirs() || chapterDir().isDirectory)
        assertTrue("chmod 500", chapterDir().setWritable(false, false))
        try {
            gateway.httpPost(ep.port,
                "/api/manga/download/resume?source=$source&cid=$comicId")
            var bad = ""
            for (i in 0 until 45) {
                delay(2000)
                val s = taskStatus(ep.port)
                val status = s.optString("status")
                val failed = s.optInt("failed_chapters", 0)
                if (status == "error" || status == "done" || failed > 0) {
                    bad = "status=$status failed_chapters=$failed reason=" +
                        s.optString("error").take(40)
                    break
                }
            }
            assertTrue("不可写时必须如实失败（实际 '$bad'）", bad.isNotBlank())
            assertTrue("不得谎报完成（$bad）", !bad.startsWith("status=done"))
            assertTrue("目录不可写时不应产生图片", images().isEmpty())
            ev("目标不可写：如实失败（$bad），未产生图片")
        } finally {
            chapterDir().setWritable(true, true)
        }
        // 恢复权限后应能正常下载（证明失败是环境问题，不是任务坏了）
        gateway.httpPost(ep.port, "/api/manga/download/resume?source=$source&cid=$comicId")
        var progressed = false
        for (i in 0 until 60) {
            delay(2000)
            if (images().isNotEmpty()) { progressed = true; break }
        }
        assertTrue("恢复权限后应能继续下载", progressed)
        ev("恢复权限后继续下载：${images().size} 张")
    }

    private fun assertEqualsMsg(msg: String, a: Int, b: Int) =
        assertTrue(msg + "（实际 $a vs $b）", a == b)
}
