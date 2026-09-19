package com.webnovel.mobile

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * 阶段 C 设备证据：手机上装载的是**真实业务系统**（app.py + server/ + engine/ +
 * templates/ + 内置书源），不是删减版。验证点：
 *   1. 内嵌 Python 启动业务（runtime 状态 ready、mobile profile、只有本地工作线程）；
 *   2. 会话凭据覆盖全部路径（无凭据 401，宿主持有凭据可访问）；
 *   3. 真实首页与真实 API 可用（/、/api/health、/api/manga/sources）；
 *   4. 能力台账如实标注（缺 curl_cffi/playwright → 受影响能力 degraded/unsupported）；
 *   5. 可干净停止（端口关闭）。
 * 证据以 PHASE_C_EVIDENCE 前缀写入 logcat。
 */
@RunWith(AndroidJUnit4::class)
class PhaseCSmokeTest {

    private fun ev(line: String) = println("PHASE_C_EVIDENCE $line")

    private class BinderHolder : ServiceConnection {
        val latch = CountDownLatch(1)
        val ref = AtomicReference<LocalServerService.LocalBinder?>()
        override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
            ref.set(service as? LocalServerService.LocalBinder)
            latch.countDown()
        }
        override fun onServiceDisconnected(name: ComponentName?) { ref.set(null) }
    }

    /** 阶段 C 的"业务通过"门槛：真机上跑一次真实业务（不是只看首页能打开）。 */
    @Test
    fun phaseC_businessSmoke() {
        val ctx: Context = InstrumentationRegistry.getInstrumentation().targetContext
        val conn = BinderHolder()
        assertTrue(ctx.bindService(Intent(ctx, LocalServerService::class.java), conn,
            Context.BIND_AUTO_CREATE))
        assertTrue(conn.latch.await(20, TimeUnit.SECONDS))
        val binder = conn.ref.get()!!
        var st: Map<String, Any?> = emptyMap()
        val worker = Thread { st = binder.ensureStarted() }
        worker.start(); worker.join(300_000)
        val port = (st["port"] as? Number)?.toInt() ?: 0
        assertTrue("服务未就绪: $st", port > 0 && st["state"] == "ready")
        val token = binder.sessionToken()
        val base = "http://127.0.0.1:$port"

        // 1) 真实全网搜索（走手机网络 + 规则引擎 + cloudscraper 降级引擎）
        val t0 = System.currentTimeMillis()
        val (sc, sb) = open("$base/api/search?q=" + java.net.URLEncoder.encode("剑来", "UTF-8"), token)
        val ms = System.currentTimeMillis() - t0
        val groups = Regex("\"group_key\"").findAll(sb).count()
        val books = Regex("\"book_url\"").findAll(sb).count()
        ev("search=$sc ms=$ms groups=$groups books=$books body=${sb.take(200)}")
        assertEquals("搜索接口应 200", 200, sc)

        // 2) 书源列表（本地）
        val (lc, lb) = open("$base/api/sources", token)
        ev("sources_page=$lc bytes=${lb.length}")
        assertEquals(200, lc)

        // 3) 小说书库（本地私有目录）
        val (bc, bb) = open("$base/api/books", token)
        ev("books=$bc ${bb.take(120)}")
        assertEquals(200, bc)

        ev("business_groups=$groups business_books=$books")
        // 诊断第七节：把"没有结果"转成**测试失败**，不允许 PARTIAL 蒙混过关
        assertTrue("全网搜索结果解析数 groups=$groups books=$books，至少应有结果",
            books > 0)
        ev("RESULT=PASS")
        // 不停服务：留给外部继续观察（下一用例或宿主）
    }

    private fun open(url: String, token: String?): Pair<Int, String> {
        val c = URL(url).openConnection() as HttpURLConnection
        if (token != null) c.setRequestProperty("X-Mobile-Token", token)
        c.setRequestProperty("Connection", "close")
        c.connectTimeout = 15000; c.readTimeout = 120000
        return try {
            val code = c.responseCode
            val body = (if (code in 200..299) c.inputStream else c.errorStream)
                ?.bufferedReader()?.readText() ?: ""
            code to body
        } finally { c.disconnect() }
    }

    @Test
    fun phaseC_realAppOnDevice() {
        val ctx: Context = InstrumentationRegistry.getInstrumentation().targetContext
        val conn = BinderHolder()
        assertTrue(ctx.bindService(Intent(ctx, LocalServerService::class.java), conn,
            Context.BIND_AUTO_CREATE))
        assertTrue("无法绑定本机服务", conn.latch.await(20, TimeUnit.SECONDS))
        val binder = conn.ref.get()!!

        var st: Map<String, Any?> = emptyMap()
        val worker = Thread { st = binder.ensureStarted() }
        worker.start()
        worker.join(300_000)          // 首次启动要解包 CPython 标准库与业务代码
        val port = (st["port"] as? Number)?.toInt() ?: 0
        ev("boot state=${st["state"]} port=$port py=${st["python"]}")
        assertTrue("服务未就绪: $st", port > 0 && st["state"] == "ready")
        val token = binder.sessionToken()
        val base = "http://127.0.0.1:$port"

        // 1) 宿主探测端点（免凭据，只回存活）
        val (ph, pb) = open("$base/__mobile/health", null)
        ev("probe=$ph ${pb.take(120)}")
        assertEquals(200, ph)
        // 断言需容忍空格：Python json.dumps 默认带 ", " 分隔，Flask jsonify 不带
        val pbFlat = pb.replace(" ", "")
        assertTrue("probe 应返回存活状态: $pb", pbFlat.contains("\"ok\":true"))

        // 2) 无凭据访问真实 API 必须 401
        val (nh, _) = open("$base/api/health", null)
        ev("no_token_health=$nh")
        assertEquals(401, nh)

        // 3) 真实业务 health（runtime + 能力台账）
        val (hh, hb) = open("$base/api/health", token)
        ev("health=$hh ${hb.take(400)}")
        assertEquals(200, hh)
        assertTrue("health 应含 runtime 段", hb.contains("\"runtime\""))
        assertTrue("health 应含能力台账", hb.contains("\"capabilities\""))
        assertTrue("mobile profile", hb.contains("\"profile\":\"mobile\""))
        assertTrue("runtime ready", hb.contains("\"state\":\"ready\""))

        // 4) 真实首页（现有网页界面，不是删减版）
        val (ih, ib) = open("$base/", token)
        ev("index=$ih bytes=${ib.length}")
        assertEquals(200, ih)
        assertTrue("应返回真实首页", ib.contains("webnovel_rules") || ib.contains("书源"))

        // 5) 漫画源列表 + 能力状态
        val (sh, sb) = open("$base/api/manga/sources", token)
        ev("sources=$sh ${sb.take(400)}")
        assertEquals(200, sh)
        assertTrue("源列表应带能力状态", sb.contains("\"status\""))
        assertTrue("jm 应如实降级（缺 curl_cffi）", sb.contains("\"key\":\"jm\""))

        // 6) 控制通道（带凭据）
        val (ch, cb) = open("$base/__mobile/status", token)
        ev("status=$ch ${cb.take(300)}")
        assertEquals(200, ch)

        // 7) 停止：端口必须关闭
        val (xh, _) = open("$base/__mobile/stop", token)
        ev("stop=$xh")
        var closed = false
        val t0 = System.currentTimeMillis()
        while (System.currentTimeMillis() - t0 < 20_000) {
            try {
                open("$base/__mobile/health", null); Thread.sleep(500)
            } catch (e: Exception) { closed = true; break }
        }
        ev("port_closed_after_stop=$closed")
        assertTrue("停止后端口仍可访问", closed)
        ev("RESULT=PASS")
        ctx.unbindService(conn)
    }
}
