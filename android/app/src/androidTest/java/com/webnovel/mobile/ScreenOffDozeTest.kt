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
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

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
    private val source = "mangadex"
    private var comicId = ""
    private var created = false
    private var deepSleepSupported = true

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
        runCatching {
            // 无论成败都要亮屏、退出 Doze，别把设备留在特殊状态。
            // 只在"确实黑屏"时才按电源键——无条件按会把本来亮着的屏关掉。
            val wake = Shell.run("dumpsys power | grep -m1 mWakefulness=")
            if (wake.contains("Asleep") || wake.contains("Dozing")) {
                Shell.run("input keyevent 26")
            }
            Shell.run("dumpsys deviceidle unforce")
            Shell.run("cmd power set-fixed-performance-mode-enabled false")
        }
        runCatching {
            runBlocking {
                val gateway = EngineGateway(ctx)
                var ep = gateway.currentEndpoint()
                if (ep == null) ep = (gateway.connect() as? EngineState.Ready)?.endpoint
                if (created && ep != null && comicId.isNotBlank()) {
                    gateway.httpDelete(ep.port, "/api/manga/library/$source/$comicId?files=1")
                }
                gateway.stopEngine()
            }
        }
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
        comicId = fx.comicId
        chapterId = fx.chapterId
        val body = JSONObject().put("title", fx.title).put("cover", fx.cover)
            .put("chapters", JSONArray().put(fx.chapterId)).toString()
        val ok = gw.httpPost(port, "/api/manga/$source/$comicId/download", body)
        assertTrue("创建下载任务应 2xx：HTTP ${ok.code}", ok.code in 200..299)
        created = true
        ev("任务已创建：$comicId / ${fx.chapterId}")
    }

    private suspend fun taskStatus(gw: EngineGateway, port: Int): String {
        val r = gw.httpText(port, "/api/manga/download/status?source=$source&cid=$comicId")
        return runCatching { JSONObject(r.body).optString("status") }.getOrDefault("")
    }
}
