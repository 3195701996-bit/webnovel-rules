package com.webnovel.mobile

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Binder
import android.os.Build
import android.os.IBinder
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import java.security.SecureRandom

/**
 * 本机服务器：在私有 :server 进程里跑内嵌 Python（Chaquopy）+ WSGI(Waitress)。
 *
 * 设计对齐点：
 *  - 前台服务类型按用户发起的下载/数据传输评估为 dataSync（§6.2）；
 *  - 先满足前台通知要求，再异步初始化 Python，避免启动阶段阻塞（§6.1）；
 *  - onTimeout（Android 15+，dataSync 后台累计 6 小时）到来时立刻停止派发、
 *    有界清理并停止服务，把原因落盘供下次界面展示（§6.2）——不重启刷额度；
 *  - START_NOT_STICKY：不做开机自启/后台自行拉起（§6.2/§6.4）；
 *  - 停止时落盘并关闭监听，重复调用安全（§5）。
 */
class LocalServerService : Service() {

    companion object {
        private const val TAG = "MobileServer"
        const val ACTION_START = "com.webnovel.mobile.action.START"
        const val ACTION_STOP = "com.webnovel.mobile.action.STOP"
        const val CHANNEL_ID = "mobile_server"
        const val NOTIF_ID = 1001
        const val PREFS = "mobile_server"
        const val KEY_PORT = "port"
        const val KEY_LAST_REASON = "last_stop_reason"
        const val DEFAULT_PORT = 8766
    }

    inner class LocalBinder : Binder() {
        fun ensureStarted(): Map<String, Any?> = startServerIfNeeded()
        fun status(): Map<String, Any?> = currentStatus()
        fun stop(reason: String): Map<String, Any?> = stopServer(reason)
        fun sessionToken(): String = token
        fun port(): Int = port
        fun lastReason(): String = lastStopReason
    }

    private val binder = LocalBinder()
    @Volatile private var runtime: PythonRuntime? = null
    @Volatile private var token: String = ""
    @Volatile private var port: Int = 0
    @Volatile private var stateDesc: String = "stopped"
    @Volatile private var lastStopReason: String = ""
    private var starting = false

    override fun onBind(intent: Intent?): IBinder = binder

    override fun onCreate() {
        super.onCreate()
        createChannel()
        // 先满足前台通知要求（5 秒内），Python 初始化放到后台线程。
        // 容错：Android 12+ 若在后台启动前台服务会被平台拒绝（ForegroundService-
        // StartNotAllowedException）；此时**不应让服务进程崩溃**——服务照常提供
        // 绑定能力，前台化与配额策略在阶段 D 按设计单独处理。
        try {
            startForeground(NOTIF_ID, buildNotification("正在启动本机服务…"))
        } catch (t: Throwable) {
            Log.w(TAG, "前台化被平台拒绝（服务继续以绑定方式运行）: ${t.javaClass.simpleName}")
        }
        lastStopReason = prefs().getString(KEY_LAST_REASON, "") ?: ""
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> stopServer("user_notification")
            else -> ensureForegroundAndStart()
        }
        return START_NOT_STICKY   // 不自动复活（设计 §6.2）
    }

    override fun onDestroy() {
        // 有界清理；Activity 重建或系统回收时都不留孤儿线程/端口
        try { runtime?.stop("service_destroy") } catch (t: Throwable) { Log.w(TAG, "stop on destroy: $t") }
        super.onDestroy()
    }

    /** Android 15+：dataSync 前台服务后台配额耗尽回调（设计 §6.2） */
    override fun onTimeout(startId: Int, fgsType: Int) {
        Log.w(TAG, "前台服务超时（fgsType=$fgsType），停止派发并做有界清理")
        recordReason("fgs_timeout")
        stopServer("fgs_timeout")
    }

    private fun ensureForegroundAndStart() {
        if (starting) return
        starting = true
        Thread({
            try { startServerIfNeeded() } catch (t: Throwable) {
                Log.e(TAG, "启动失败: ${t.javaClass.name}: ${t.message}")
                t.stackTrace.take(12).forEach { Log.e(TAG, "   at $it") }
                recordReason("start_failed:${t.javaClass.simpleName}")
            } finally { starting = false }
        }, "python-boot").start()
    }

    @Synchronized
    private fun startServerIfNeeded(): Map<String, Any?> {
        runtime?.let { rt ->
            val st = rt.status()
            if (st["state"] == "ready") return st
        }
        Log.i(TAG, "步骤1 开始：Python.isStarted=${Python.isStarted()}")
        if (!Python.isStarted()) {
            val t0 = System.currentTimeMillis()
            Python.start(AndroidPlatform(applicationContext))
            Log.i(TAG, "步骤2 Python 运行时就绪，用时 ${System.currentTimeMillis() - t0}ms")
        }
        val py = Python.getInstance()
        val mod = py.getModule("server.mobile_entry")
        Log.i(TAG, "步骤3 入口模块已加载")
        val rt = PythonRuntime(mod.callAttr("get_runtime"))
        runtime = rt

        // 会话凭据：每次启动随机生成，只在本应用内部传递（不进 URL/日志）
        token = randomToken()
        // 端口：沿用上次端口以保持 WebView origin（设计 §5.3）
        val preferred = prefs().getInt(KEY_PORT, DEFAULT_PORT)

        val dataDir = File(filesDir, "runtime").absolutePath
        val cacheDir = cacheDir.absolutePath
        // 内置书源：从 APK 的 assets/sources 解到应用私有目录（设计 §7：
        // 首启只复制经审核的初始书源，且不覆盖用户已有修改）
        // 解压内置书源：与备份共用同一实现（BundledSources），幂等
        val srcDir = File(dataDir, "sources")
        val srcNew = BundledSources.ensure(this, srcDir)
        // 按内置源种子清单**逐文件合并**启用状态：只动"与出厂内容一致"的文件，
        // 用户改过的一律跳过（升级合并策略，方向基线 §6.4）
        val seeded = BundledSources.applySeed(this, srcDir)
        Log.i(TAG, "步骤4 初始化数据目录 $dataDir（本次解出书源 $srcNew 个，" +
            "现有 ${BundledSources.count(srcDir)} 个，种子改动 $seeded 个）")
        rt.initialize(dataDir, cacheDir, token, preferred)
        Log.i(TAG, "步骤5 初始化完成，启动 WSGI 服务")
        val st = rt.start()
        port = (st["port"] as? Number)?.toInt() ?: 0
        stateDesc = st["state"] as? String ?: "unknown"
        if (port > 0) prefs().edit().putInt(KEY_PORT, port).apply()
        updateNotification(if (stateDesc == "ready") "运行中 · 127.0.0.1:$port" else "启动失败：${st["error"]}")
        Log.i(TAG, "服务状态=$stateDesc port=$port instance=${st["instance_id"]}")
        return st
    }

    @Synchronized
    private fun stopServer(reason: String): Map<String, Any?> {
        val rt = runtime
        val st = try { rt?.stop(reason) ?: mapOf("state" to "stopped") }
                 catch (t: Throwable) { Log.w(TAG, "stop: $t"); mapOf("state" to "stopped") }
        stateDesc = st["state"] as? String ?: "stopped"
        recordReason(reason)
        stopForegroundCompat()
        stopSelf()
        return st
    }

    private fun currentStatus(): Map<String, Any?> = try {
        runtime?.status() ?: mapOf("state" to "stopped", "port" to 0, "token" to "")
    } catch (t: Throwable) { mapOf("state" to "failed", "error" to t.toString()) }

    private fun recordReason(reason: String) {
        lastStopReason = reason
        prefs().edit().putString(KEY_LAST_REASON, reason).apply()
    }

    /** 把 assets 里 sources 目录下的书源 json 解到私有目录；已存在不覆盖 */

    private fun prefs() = getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun randomToken(): String {
        val b = ByteArray(24); SecureRandom().nextBytes(b)
        return b.joinToString("") { "%02x".format(it) }
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(NotificationChannel(
                CHANNEL_ID, getString(R.string.notif_channel),
                NotificationManager.IMPORTANCE_LOW).apply {
                    description = getString(R.string.notif_channel_desc)
                })
        }
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val stop = PendingIntent.getService(
            this, 1, Intent(this, LocalServerService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE)
        val b = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            Notification.Builder(this, CHANNEL_ID) else @Suppress("DEPRECATION") Notification.Builder(this)
        return b.setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null, "停止", stop).build())
            .build()
    }

    private fun updateNotification(text: String) {
        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIF_ID, buildNotification(text))
    }

    private fun stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) stopForeground(STOP_FOREGROUND_REMOVE)
        else @Suppress("DEPRECATION") stopForeground(true)
    }
}
