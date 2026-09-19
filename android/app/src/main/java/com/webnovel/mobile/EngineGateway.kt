package com.webnovel.mobile

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import android.util.Log
import android.webkit.CookieManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.atomic.AtomicInteger
import kotlin.coroutines.resume

/**
 * 引擎端点：本次启动的端口、会话凭据与实例标识。
 * 界面层只持有它，不直接关心服务启停/端口/认证（设计 §5 的"引擎网关"）。
 */
data class EngineEndpoint(val instanceId: String, val port: Int, val token: String,
                          val imagePort: Int = port) {
    val origin: String get() = "http://127.0.0.1:$port"
    /** 图片流量专用端口（与 API 隔离，图片回源占线也堵不到 API）；无独立端口时回退主端口 */
    val imageOrigin: String get() = "http://127.0.0.1:$imagePort"
}

/** 引擎对外状态：界面据此渲染 loading / ready / error+重试，而不是黑屏或打印对象 */
sealed interface EngineState {
    data object Idle : EngineState
    data object Starting : EngineState
    data class Ready(val endpoint: EngineEndpoint) : EngineState
    data class Failed(val stage: String, val detail: String) : EngineState
}

/** HTTP 文本结果：**带状态码**；非 2xx 读 errorStream，不再统一吞成异常类名 */
data class HttpText(val code: Int, val body: String) {
    val ok: Boolean get() = code in 200..299
}

/**
 * 本机引擎网关（P0-1/P0-2/P0-3/P1-1 的修复点集中在这里）：
 *
 * - **统一契约**：存活探测用 `/__mobile/health`（中间件直接应答），不再请求业务侧
 *   不存在的 `/health`；失败时给出阶段（bind/start/probe/verify/auth/load）与状态码。
 * - **结构化解析**：用 JSONObject 读 `ok`/`state`，不依赖空格、键顺序或排版
 *   （旧实现用 `contains("\"ok\":true")` 在带空格的 JSON 上永远不成立）。
 * - **真身份校验**：存活只证明"有服务应答"；随后用**带凭据**的 `/__mobile/status`
 *   取 Python 侧 `instance_id`，与 Binder 拿到的实例比对，一致才加载内容。
 * - **完整重试链**：ensureStarted → 取端口/凭据/实例 → 探测 → 身份校验 → 认证 Cookie
 *   → 加载；所有入口（首启/重试/重载）复用同一条链。
 * - **代次守卫**：并发的连接/停止/重连，旧异步结果不得覆盖新状态。
 * - 日志只记路径/状态码/阶段，**不记 token**。
 */
class EngineGateway(private val appContext: Context) {

    companion object {
        private const val TAG = "EngineGateway"
        const val PROBE_PATH = "/__mobile/health"
        const val STATUS_PATH = "/__mobile/status"
        const val COOKIE_NAME = "mobile_session"
    }

    private val gen = AtomicInteger(0)
    @Volatile private var binder: LocalServerService.LocalBinder? = null
    @Volatile private var binding = false
    @Volatile private var endpoint: EngineEndpoint? = null

    private val conn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
            binder = service as? LocalServerService.LocalBinder
            binding = false
            Log.i(TAG, "已连接本机服务进程")
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            binder = null
            binding = false
            endpoint = null          // 断开即清理运行描述，避免重载打到旧地址
            Log.w(TAG, "服务进程断开")
        }
    }

    fun currentEndpoint(): EngineEndpoint? = endpoint

    /** 统一连接流程；可重复调用，并发由代次守卫收敛 */
    suspend fun connect(onStage: ((String) -> Unit)? = null): EngineState {
        val mine = gen.incrementAndGet()
        endpoint = null
        return try {
            onStage?.invoke("正在启动本机引擎")
            val b = ensureBound(mine) ?: return fail(mine, "bind", "无法绑定本机服务进程")
            val started = withContext(Dispatchers.IO) { b.ensureStarted() }
            if (gen.get() != mine) return EngineState.Idle
            val state = started["state"] as? String
            if (state != "ready") {
                return fail(mine, "start",
                    "引擎未就绪: state=$state error=${started["error"] ?: "-"}")
            }
            val port = (started["port"] as? Number)?.toInt() ?: 0
            val token = b.sessionToken()
            if (port <= 0 || token.isEmpty()) {
                return fail(mine, "start", "端口或会话凭据缺失")
            }
            onStage?.invoke("正在校验本机服务")
            val probe = request("GET", port, PROBE_PATH, null, auth = false, tokenOverride = token)
            if (!probe.ok) {
                return fail(mine, "probe", "$PROBE_PATH -> HTTP ${probe.code}")
            }
            val pj = runCatching { JSONObject(probe.body) }.getOrNull()
            if (pj == null || !pj.optBoolean("ok") || pj.optString("state") != "ready") {
                return fail(mine, "probe", "探测响应异常: ${probe.body.take(120)}")
            }
            if (pj.optString("state") != "ready") return fail(mine, "probe", "服务状态非 ready")
            // 身份校验：带凭据读 Python 侧实例标识，和 Binder 实例比对
            val st = request("GET", port, STATUS_PATH, null, auth = true, tokenOverride = token)
            if (!st.ok) return fail(mine, "verify", "$STATUS_PATH -> HTTP ${st.code}")
            val sj = runCatching { JSONObject(st.body) }.getOrNull()
            val instanceId = sj?.optJSONObject("runtime")?.optString("instance_id") ?: ""
            val fromBinder = (started["instance_id"] as? String) ?: ""
            if (instanceId.isEmpty() || (fromBinder.isNotEmpty() && instanceId != fromBinder)) {
                return fail(mine, "verify",
                    "实例标识不一致（binder=${fromBinder.take(8)} status=${instanceId.take(8)}）")
            }
            if (gen.get() != mine) return EngineState.Idle
            // 图片流量独立端口（双实例隔离；旧包没有该字段时回退主端口）
            val imagePort = (started["image_port"] as? Number)?.toInt() ?: port
            val ep = EngineEndpoint(instanceId, port, token, imagePort)
            endpoint = ep
            Log.i(TAG, "引擎就绪 port=$port instance=${instanceId.take(8)}")
            EngineState.Ready(ep)
        } catch (c: kotlinx.coroutines.CancellationException) {
            // 协程取消（界面销毁/离开组合）**不是引擎故障**：必须原样抛出，
            // 否则会把取消记录成失败并在状态里残留异常（实测踩到）。
            throw c
        } catch (t: Throwable) {
            fail(mine, "exception", "${t.javaClass.simpleName}: ${t.message}")
        }
    }

    private fun fail(mine: Int, stage: String, detail: String): EngineState {
        Log.w(TAG, "连接失败 stage=$stage detail=$detail")
        return if (gen.get() == mine) EngineState.Failed(stage, detail) else EngineState.Idle
    }

    private suspend fun ensureBound(mine: Int): LocalServerService.LocalBinder? {
        binder?.let { return it }
        suspendCancellableCoroutine<Unit> { cont ->
            binding = true
            val ok = appContext.bindService(
                Intent(appContext, LocalServerService::class.java),
                conn, Context.BIND_AUTO_CREATE)
            if (!ok) {
                binding = false
                cont.resume(Unit)
            } else {
                // 连接回调到来即继续；超时用上层重试兜底
                cont.resume(Unit)
            }
        }
        // 等服务进程回调就绪（最多 ~8s；首次启动 Python 由 ensureStarted 自己等）
        repeat(160) {
            if (gen.get() != mine) return null
            binder?.let { return it }
            kotlinx.coroutines.delay(50)
        }
        return binder
    }

    suspend fun stopEngine(): Boolean = withContext(Dispatchers.IO) {
        gen.incrementAndGet()
        endpoint = null
        val b = binder
        val ok = runCatching { b?.stop("ui") }.isSuccess
        Log.i(TAG, "已请求停止引擎 ok=$ok")
        ok
    }

    /**
     * 把会话凭据写入 WebView Cookie（**等待回调成功**再加载，避免首请求 401 竞态）。
     *
     * 属性说明：Path=/ 限定作用域；SameSite=Lax 允许应用内顶层导航但不能被跨站请求携带；
     * HttpOnly 让网页脚本（含被抓取源页面里的脚本）无法用 document.cookie 读到凭据——
     * 该 Cookie 只由服务端读取，前端不依赖它，故不牺牲功能。
     * 不加 Secure：连接是本机回环 http://127.0.0.1，加 Secure 会导致 Cookie 根本不被发送。
     */
    suspend fun installCookie(ep: EngineEndpoint): Boolean =
        suspendCancellableCoroutine { cont ->
            val cm = CookieManager.getInstance()
            cm.setAcceptCookie(true)
            try {
                cm.setCookie(ep.origin, "$COOKIE_NAME=${ep.token}; Path=/; HttpOnly; SameSite=Lax") {
                    cm.flush()
                    cont.resume(true)
                }
            } catch (t: Throwable) {
                Log.w(TAG, "写 Cookie 失败: ${t.javaClass.simpleName}")
                cont.resume(false)
            }
        }

    /** GET：读接口（书架、目录、章节正文）。 */
    suspend fun httpText(port: Int, path: String, auth: Boolean = true): HttpText =
        request("GET", port, path, null, auth)

    /**
     * POST JSON：写接口（保存阅读进度、重爬单章）。
     *
     * 重爬会真的去抓一章（服务端 timeout=25s），因此 POST 的读超时放宽到 45s；
     * 其余与 GET 共用同一条链路（认证、代次校验、状态码与 errorStream 处理）。
     */
    suspend fun httpPost(port: Int, path: String, json: String? = null,
                         auth: Boolean = true): HttpText =
        request("POST", port, path, json, auth)

    /** DELETE：删除书源等资源（与 GET/POST 共用认证与代次处理） */
    suspend fun httpDelete(port: Int, path: String, auth: Boolean = true): HttpText =
        request("DELETE", port, path, null, auth)

    /**
     * 读取 SSE（text/event-stream）：每收到一条 `data: {...}` 就回调一次，
     * 用于"结果随源到达逐条出现"，而不是等所有源都跑完。
     *
     * 与 request() 的区别：**不缓冲整个响应体**，边读边回调；返回 HTTP 状态码
     * （-1 表示连接层失败）。取消协程时会断开连接（不会泄漏读线程）。
     * 调用方拿到事件后自行解析 JSON——服务端事件格式见 /api/manga/search/stream。
     */
    suspend fun streamEvents(port: Int, path: String,
                             readTimeoutMs: Int = 60_000,
                             onEvent: (JSONObject) -> Unit): Int =
        withContext(Dispatchers.IO) {
            val mine = gen.get()
            val token = endpoint?.token ?: ""
            val url = URL("http://127.0.0.1:$port$path")
            val c = url.openConnection() as HttpURLConnection
            try {
                c.requestMethod = "GET"
                c.connectTimeout = 8000
                c.readTimeout = readTimeoutMs
                c.setRequestProperty("Connection", "close")
                c.setRequestProperty("Accept", "text/event-stream")
                if (token.isNotEmpty()) c.setRequestProperty("X-Mobile-Token", token)
                // 设备级联网信号：服务端据此说清"本机没网"而不是"源站全坏了"
                NetState.header(appContext).takeIf { it.isNotEmpty() }
                    ?.let { c.setRequestProperty("X-Device-Net", it) }
                val code = c.responseCode
                if (code !in 200..299) {
                    Log.w(TAG, "GET $path 流式失败: HTTP $code")
                    return@withContext code
                }
                val sb = StringBuilder()
                c.inputStream.bufferedReader(Charsets.UTF_8).use { br ->
                    while (true) {
                        currentCoroutineContext().ensureActive()   // 取消即断开
                        val line = br.readLine() ?: break
                        if (line.startsWith("data:")) {
                            sb.append(line.removePrefix("data:").trim())
                        } else if (line.isBlank()) {
                            if (sb.isNotEmpty()) {
                                val o = runCatching { JSONObject(sb.toString()) }.getOrNull()
                                if (o != null) onEvent(o)
                                sb.setLength(0)
                            }
                        }
                    }
                }
                if (gen.get() != mine) return@withContext -1
                code
            } catch (cx: kotlinx.coroutines.CancellationException) {
                throw cx
            } catch (t: Throwable) {
                Log.w(TAG, "GET $path 流式读取失败: ${t.javaClass.simpleName}: ${t.message}")
                -1
            } finally {
                runCatching { c.disconnect() }
            }
        }

    private suspend fun request(method: String, port: Int, path: String, json: String?,
                                auth: Boolean, tokenOverride: String? = null): HttpText =
        withContext(Dispatchers.IO) {
        val mine = gen.get()
        // 连接过程中 endpoint 尚未赋值，此时必须由调用方显式传入本次凭据，
        // 否则带认证的 /__mobile/status 会以空 token 发出并得到 401。
        val token = tokenOverride ?: (endpoint?.token ?: "")
        val url = URL("http://127.0.0.1:$port$path")
        val c = url.openConnection() as HttpURLConnection
        try {
            c.requestMethod = method
            c.connectTimeout = 8000
            c.readTimeout = if (method == "POST") 45000 else 30000
            c.setRequestProperty("Connection", "close")
            if (auth && token.isNotEmpty()) c.setRequestProperty("X-Mobile-Token", token)
            NetState.header(appContext).takeIf { it.isNotEmpty() }
                ?.let { c.setRequestProperty("X-Device-Net", it) }
            if (json != null) {
                c.doOutput = true
                c.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                c.outputStream.use { it.write(json.toByteArray(Charsets.UTF_8)) }
            }
            val code = c.responseCode
            val stream = if (code in 200..299) c.inputStream else c.errorStream
            val body = stream?.bufferedReader()?.use { it.readText() } ?: ""
            if (gen.get() != mine) return@withContext HttpText(-1, "")
            Log.i(TAG, "$method $path -> $code (${body.length}B)")
            HttpText(code, body)
        } catch (cx: kotlinx.coroutines.CancellationException) {
            throw cx      // 取消不是失败，原样抛出（与 connect 一致）
        } catch (t: Throwable) {
            // 不把异常当"成功"；把阶段与原因交给界面展示。
            // 失败原因必须放进 **JSON 的 error 字段**：界面统一用
            // JSONObject(body).optString("error") 取原因，若这里塞的是裸字符串，
            // 超时这种最常见的失败在界面上就只剩"HTTP -1"、没有任何原因
            // （实测：禁漫分类取数超时 → 界面显示"读取分类失败：HTTP 0 · "）。
            val why = when (t) {
                is java.net.SocketTimeoutException ->
                    "请求超时（引擎 ${if (method == "POST") 45 else 30} 秒内未返回）：" +
                        "目标源站可能不可达或响应过慢"
                is java.net.ConnectException ->
                    "无法连接本机引擎（端口未监听）：引擎可能正在启动或已退出"
                is java.net.UnknownHostException ->
                    "域名解析失败（DNS 不可用）"
                else -> t.javaClass.simpleName + ": " + (t.message ?: "")
            }
            Log.w(TAG, "$method $path 失败: ${t.javaClass.simpleName}: ${t.message}")
            HttpText(-1, "{\"ok\":false,\"error\":${JSONObject.quote(why)}}")
        } finally {
            runCatching { c.disconnect() }
        }
    }
}
