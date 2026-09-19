package com.webnovel.mobile

import com.chaquo.python.PyObject

/**
 * 内嵌 Python 运行时的 Kotlin 侧句柄（阶段 A）。
 *
 * 约束（设计 §5）：
 *  - Python 只在 :server 进程初始化一次，Activity 绝不调用 Python 初始化；
 *  - 所有调用都不阻塞 Android 主线程（由调用方放到 IO 线程）；
 *  - 状态查询与启动/停止都经过这一个入口，避免出现第二个服务实例。
 */
class PythonRuntime(private val runtime: PyObject) {

    /** 只做初始化与目录校验：不联网、不起服务。 */
    fun initialize(dataDir: String, cacheDir: String, token: String, port: Int): Map<String, Any?> =
        runtime.callAttr("initialize", dataDir, cacheDir, token, port).asMap().toStringMap()

    /** 创建唯一的 WSGI 服务与工作线程。 */
    fun start(): Map<String, Any?> = runtime.callAttr("start").asMap().toStringMap()

    fun status(): Map<String, Any?> = runtime.callAttr("status").asMap().toStringMap()

    fun stop(reason: String): Map<String, Any?> = runtime.callAttr("stop", reason).asMap().toStringMap()

    private fun Map<PyObject, PyObject>.toStringMap(): Map<String, Any?> =
        entries.associate { (k, v) -> k.toString() to v.toJava(Any::class.java) }
}
