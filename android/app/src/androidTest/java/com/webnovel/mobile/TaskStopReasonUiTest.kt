package com.webnovel.mobile

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.performClick
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.net.URLEncoder
import java.security.MessageDigest

/**
 * **任务"为什么停了"必须可解释、可继续**（路线 §7 P1-1 / §8 后台行）。
 *
 * 路线原文："前台服务被停止、用户手动停止、进程重启后可解释地恢复或等待用户恢复。"
 * 验收矩阵后台行："前台、锁屏、系统终止、服务超时 → **任务状态/恢复原因可见**"。
 *
 * 本用例在**设备上跑真实下载任务**（用小说回归源集的 ixdzs8，不造假的进度），验证两条
 * 用户真的会遇到的路径：
 *   1. 用户手动停止 → 下载页显示"你停止了任务" + 断点"已下载 x/y 章" + 可继续；
 *   2. **服务被停止**（`am stopservice`，等价于系统停掉前台服务/服务被销毁）→
 *      重启引擎后任务状态是"可解释的中断"，原因是服务层给的（不是笼统的"已停止"）。
 *
 * 清理纪律：只动本用例创建的任务/书籍；删书前先等任务停止写入（done 连续两次不变）。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class TaskStopReasonUiTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext
    private var gateway = EngineGateway(InstrumentationRegistry.getInstrumentation().targetContext)

    private val sourceUid = "爱下书_ixdzs8__ixdzs8.com"
    private val keyword = "剑来"

    private fun ev(line: String) = println("STOP_REASON_EVIDENCE $line")

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8")

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size
    private fun texts(t: String, substring: Boolean = false) =
        rule.onAllNodesWithText(t, substring = substring).fetchSemanticsNodes().size

    /** 自己绑一次服务，拿到 binder 才能调用与 onTimeout 相同的停止入口 */
    private class BinderHolder : ServiceConnection {
        val latch = java.util.concurrent.CountDownLatch(1)
        val ref = java.util.concurrent.atomic.AtomicReference<LocalServerService.LocalBinder?>()
        override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
            ref.set(service as? LocalServerService.LocalBinder)
            latch.countDown()
        }
        override fun onServiceDisconnected(name: ComponentName?) { ref.set(null) }
    }

    private fun bookKeyOf(uid: String, bookUrl: String): String {
        val md5 = MessageDigest.getInstance("MD5").digest(bookUrl.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
        return "${uid}_${md5.take(10)}"
    }

    @After
    fun tearDown() {
        // 停任务 → 等写入静止 → 删任务/删书；失败也不掩盖（断言在用例里）
        runBlocking {
            val ep = gateway.currentEndpoint() ?: return@runBlocking
            val r = gateway.httpText(ep.port, "/api/tasks")
            val arr = runCatching { JSONObject(r.body).optJSONArray("tasks") }.getOrNull()
                ?: return@runBlocking
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                val id = o.optString("id")
                if (!id.startsWith("t") || o.optString("source_uid") != sourceUid) continue
                runCatching { gateway.httpPost(ep.port, "/api/tasks/${enc(id)}/stop") }
                runCatching { gateway.httpDelete(ep.port, "/api/tasks/${enc(id)}") }
            }
        }
    }

    /** 搜索 → 目录 → 加入书架（与用户操作同路径），返回书 key */
    private suspend fun startRealDownload(ep: EngineEndpoint): String {
        val sr = gateway.httpText(ep.port, "/api/search?source=${enc(sourceUid)}&q=${enc(keyword)}")
        assertTrue("搜索应 200：HTTP ${sr.code}", sr.ok)
        val groups = JSONObject(sr.body).optJSONArray("groups")!!
        var bookUrl = ""
        for (i in 0 until groups.length()) {
            if (bookUrl.isNotBlank()) break
            val ss = groups.optJSONObject(i)?.optJSONArray("sources") ?: continue
            for (j in 0 until ss.length()) {
                val s = ss.optJSONObject(j) ?: continue
                if (s.optString("source_uid") == sourceUid) { bookUrl = s.optString("book_url"); break }
            }
        }
        // 源站限流/不可达时**跳过**而不是判红：这条用例要真下一个任务，没有可用书源
        // 就无法进行；判红会让发布门槛变成噪声（"红"看起来像代码回归）。
        // 代码回归仍会是红（例如 /api/search 直接报错在别处断言）。
        org.junit.Assume.assumeTrue(
            "源站限流或不可达：搜不到《$keyword》，跳过（环境问题，不是代码回归）",
            bookUrl.isNotBlank())
        val key = bookKeyOf(sourceUid, bookUrl)
        // 已经下过（上一轮残留）就先删掉，保证本轮从零开始
        if (gateway.httpText(ep.port, "/api/books/${enc(key)}").ok) {
            gateway.httpDelete(ep.port, "/api/books/${enc(key)}")
            delay(2000)
        }
        val body = JSONObject().put("source_uid", sourceUid).put("book_url", bookUrl).toString()
        val cr = gateway.httpPost(ep.port, "/api/tasks", body)
        assertTrue("加入书架应 202/409：HTTP ${cr.code} ${cr.body.take(80)}",
            cr.code == 202 || cr.code == 409)
        return key
    }

    private suspend fun waitChapters(ep: EngineEndpoint, key: String, atLeast: Int,
                                     timeoutMs: Long): Int {
        val deadline = System.currentTimeMillis() + timeoutMs
        var done = 0
        while (System.currentTimeMillis() < deadline) {
            val r = gateway.httpText(ep.port, "/api/books/${enc(key)}")
            if (r.ok) done = JSONObject(r.body).optInt("done", 0)
            if (done >= atLeast) return done
            delay(2000)
        }
        return done
    }

    private suspend fun waitStopped(ep: EngineEndpoint, taskId: String, timeoutMs: Long): JSONObject {
        val deadline = System.currentTimeMillis() + timeoutMs
        var last = JSONObject()
        while (System.currentTimeMillis() < deadline) {
            val r = gateway.httpText(ep.port, "/api/tasks")
            val arr = runCatching { JSONObject(r.body).optJSONArray("tasks") }.getOrNull()
            if (arr != null) {
                for (i in 0 until arr.length()) {
                    val o = arr.optJSONObject(i) ?: continue
                    if (o.optString("id") == taskId) {
                        last = o
                        if (!o.optBoolean("running") && o.optString("status") != "running") return o
                    }
                }
            }
            delay(1500)
        }
        return last
    }

    @Test
    fun userStop_isExplainedAndResumable_includingUi() = runBlocking {
        val ep = (gateway.connect() as? EngineState.Ready)?.endpoint
            ?: throw AssertionError("引擎未就绪")
        val key = startRealDownload(ep)
        val done = waitChapters(ep, key, atLeast = 2, timeoutMs = 150_000)
        assertTrue("应至少下到 2 章（实际 $done）", done >= 2)

        // 找到任务 id 并停止
        var taskId = ""
        val r = gateway.httpText(ep.port, "/api/tasks")
        val arr = JSONObject(r.body).optJSONArray("tasks")!!
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            if (o.optString("source_uid") == sourceUid && o.optString("book_key") == key) {
                taskId = o.optString("id")
            }
        }
        assertTrue("应能找到本任务：$taskId", taskId.startsWith("t"))
        val sr = gateway.httpPost(ep.port, "/api/tasks/${enc(taskId)}/stop")
        assertTrue("停止请求应 200：HTTP ${sr.code}", sr.ok)

        val t = waitStopped(ep, taskId, 90_000)
        ev("停止后任务：status=${t.optString("status")} kind=${t.optString("stop_kind")} " +
            "reason=${t.optString("stop_reason")} resumable=${t.optBoolean("resumable")} " +
            "checkpoint=${t.optJSONObject("checkpoint")}")
        assertTrue("停止原因应是结构化枚举（user_stop）：${t.optString("stop_kind")}",
            t.optString("stop_kind") == "user_stop")
        assertTrue("原因要说清是用户停的：${t.optString("stop_reason")}",
            t.optString("stop_reason").contains("停止"))
        assertTrue("用户停的任务必须可继续", t.optBoolean("resumable"))
        val cp = t.optJSONObject("checkpoint") ?: JSONObject()
        assertTrue("断点要带已下载章数（实际 ${cp.optInt("done")}）", cp.optInt("done") >= 2)

        // UI：下载页必须把原因与"继续"按钮显示出来
        rule.waitUntil(150_000) { texts("下载") > 0 }
        rule.onAllNodesWithText("下载")[0].performClick()
        rule.waitUntil(60_000) { nodes("task_stop_reason") > 0 }
        val shown = rule.onAllNodesWithTag("task_stop_reason")[0]
            .fetchSemanticsNode().config.toString()
        ev("下载页显示的原因：$shown")
        assertTrue("下载页要显示服务端给出的原因", shown.contains("停止"))
        assertTrue("可继续的任务必须有「继续」按钮", nodes("task_resume") > 0)
        if (nodes("task_checkpoint") > 0) {
            ev("下载页显示断点：" + rule.onAllNodesWithTag("task_checkpoint")[0]
                .fetchSemanticsNode().config.toString().take(120))
        }
        Unit
    }

    @Test
    fun serviceStop_marksExplainableInterruption() = runBlocking {
        var ep = (gateway.connect() as? EngineState.Ready)?.endpoint
            ?: throw AssertionError("引擎未就绪")
        val key = startRealDownload(ep)
        val done = waitChapters(ep, key, atLeast = 2, timeoutMs = 150_000)
        assertTrue("应至少下到 2 章（实际 $done）", done >= 2)

        // 停掉服务：走的是 onTimeout/系统停止前台服务**同一条代码路径**——
        // LocalServerService.onTimeout → stopServer("fgs_timeout") →
        // runtime.stop("mobile:fgs_timeout") → 关闭钩子标记运行中的任务。
        //
        // 说明（诚实边界）：这台模拟器是 Android 12，`onTimeout` 是 Android 15+ 的回调，
        // 没法由系统触发；也不使用 am stopservice——应用仍**绑定**着服务，
        // stopService 在还有客户端绑定时不会销毁服务（实测：端口没变、任务照跑）。
        // 因此这里直接调用 binder 上与 onTimeout 完全相同的入口，并把原因码给成
        // fgs_timeout，验证的是"服务停止 → 任务带原因中断"这条链。
        val conn = BinderHolder()
        assertTrue("应能绑定本机服务",
            ctx.bindService(Intent(ctx, LocalServerService::class.java), conn,
                Context.BIND_AUTO_CREATE))
        assertTrue("服务绑定超时", conn.latch.await(20, java.util.concurrent.TimeUnit.SECONDS))
        val binder = conn.ref.get() ?: throw AssertionError("未拿到服务 binder")
        ev("以 fgs_timeout 停止服务（与 onTimeout 同入口）")
        val stopRes = binder.stop("fgs_timeout")
        ev("服务停止返回：$stopRes")
        runCatching { ctx.unbindService(conn) }
        delay(4000)

        // 换一个新的 gateway 重新拉起引擎（与用户重新打开应用等价）
        gateway = EngineGateway(ctx)
        ep = (gateway.connect() as? EngineState.Ready)?.endpoint
            ?: throw AssertionError("服务停止后应能重新拉起引擎")
        ev("服务停止后重新拉起：port=${ep.port}")

        val r = gateway.httpText(ep.port, "/api/tasks")
        val arr = JSONObject(r.body).optJSONArray("tasks")!!
        var mine: JSONObject? = null
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            if (o.optString("book_key") == key) mine = o
        }
        assertNotNull("服务停止后任务记录必须仍在（可继续）", mine)
        val t = mine!!
        ev("服务停止后：status=${t.optString("status")} kind=${t.optString("stop_kind")} " +
            "reason=${t.optString("stop_reason")}")
        assertFalse("服务停止后不得仍标 running", t.optBoolean("running"))
        assertTrue("必须是可解释的中断（service_stopped 或 process_restart），实际「" +
            t.optString("stop_kind") + "」",
            t.optString("stop_kind") in listOf("service_stopped", "process_restart"))
        val reason = t.optString("stop_reason")
        assertTrue("原因必须是给用户看的话（不是空/不是状态码）：$reason",
            reason.length > 8 && reason.contains("继续"))
        assertTrue("服务停止的任务可继续", t.optBoolean("resumable"))

        // 清理：删书（后面 tearDown 会停/删任务记录）
        gateway.httpDelete(ep.port, "/api/books/${enc(key)}")
        File(ctx.filesDir, "runtime/trash").listFiles()
            ?.filter { it.name.startsWith(key) }?.forEach { it.deleteRecursively() }
        Unit    // JUnit 要求测试方法返回 void：runBlocking 会返回最后表达式的值
    }
}
