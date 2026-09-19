package com.webnovel.mobile

import android.content.Context
import android.net.Uri
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * 导出结果：写入字节数 + 服务端给出的告警（例如全文导出陈旧标记）。
 * 失败一律抛异常，由调用方显示可读文案——不把"没写成功"当成功。
 */
data class ExportResult(val bytes: Long, val warning: String?)

/**
 * 把本机引擎的导出流写入用户通过系统文件选择器（SAF）选定的目标。
 *
 * 设计取舍：
 * - 只读本机回环地址，带会话凭据走请求头（token 不进 URL、不进文件名）。
 * - 流式拷贝（边读边写），不把整本书/整个压缩包读进内存。
 * - 非 2xx 一定失败：读 errorStream 作为错误信息。
 * - 取消（协程取消）原样抛出，不落成"导出失败"。
 */
object Export {

    private const val TAG = "Export"
    private const val BUF = 64 * 1024

    suspend fun streamToUri(
        ctx: Context,
        port: Int,
        token: String,
        path: String,
        uri: Uri,
    ): ExportResult = withContext(Dispatchers.IO) {
        val url = URL("http://127.0.0.1:$port$path")
        val c = url.openConnection() as HttpURLConnection
        try {
            c.connectTimeout = 8000
            // 打包/重建全文可能较慢，给足读超时
            c.readTimeout = 120_000
            c.setRequestProperty("Connection", "close")
            if (token.isNotEmpty()) c.setRequestProperty("X-Mobile-Token", token)
            val code = c.responseCode
            if (code !in 200..299) {
                val msg = c.errorStream?.bufferedReader()?.use { it.readText() } ?: ""
                throw IOException("导出失败：HTTP $code" + if (msg.isNotBlank()) " · ${msg.take(160)}" else "")
            }
            val warning = when {
                c.getHeaderField("X-Export-Stale") == "1" ->
                    "服务端标记：导出的全文可能不是最新（部分章节未完成/重建失败）"
                else -> null
            }
            val out = ctx.contentResolver.openOutputStream(uri, "w")
                ?: throw IOException("无法写入所选位置（系统未返回可写流）")
            var total = 0L
            out.use { o ->
                c.inputStream.use { ins ->
                    val buf = ByteArray(BUF)
                    while (true) {
                        val n = ins.read(buf)
                        if (n <= 0) break
                        o.write(buf, 0, n)
                        total += n
                    }
                }
                o.flush()
            }
            Log.i(TAG, "导出完成 path=$path bytes=$total warning=${warning != null}")
            ExportResult(total, warning)
        } catch (ce: kotlinx.coroutines.CancellationException) {
            throw ce
        } finally {
            runCatching { c.disconnect() }
        }
    }

    /** 建议文件名：去掉路径分隔等非法字符，避免 SAF 拒绝 */
    fun safeFileName(name: String, fallback: String): String {
        val cleaned = name.replace(Regex("[\\\\/:*?\"<>|\\r\\n]"), "_").trim()
        val base = cleaned.ifBlank { fallback }
        return if (base.length > 80) base.take(80) else base
    }

    /** 人类可读的字节数 */
    fun human(bytes: Long): String = when {
        bytes >= 1024 * 1024 -> String.format(java.util.Locale.US, "%.1f MB", bytes / 1048576.0)
        bytes >= 1024 -> String.format(java.util.Locale.US, "%.0f KB", bytes / 1024.0)
        else -> "$bytes 字节"
    }
}
