package com.webnovel.mobile

import android.content.Context
import android.util.Base64
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * 自检用「合成书」：在应用私有数据目录里按**真实的书目录结构**造一本三章的书，
 * 让阅读链路的验收不再依赖外部书源与网络：
 *
 *   - 第 1 章：有 .cache 正文（downloaded=true）
 *   - 第 2 章：无缓存（downloaded=false）→ 验证"尚未下载"分支
 *   - 第 3 章：在 failed 里（failed=true + 原因）
 *
 * 缓存文件名必须与服务端 cache_key_of 的规则一致：
 * 非 [\w\u4e00-\u9fff-] 的字符替换为 '_'，取后 60 字符。
 * 这里只用 ASCII URL，因此 Java 的 \w（ASCII）与 Python 的结果一致。
 *
 * 测试结束必须 [cleanup]：删掉书目录、把全局阅读进度文件恢复原样。
 */
class SelfTestBook(private val key: String = "自检_reader_path") {

    companion object {
        private const val URL1 = "https://selftest.invalid/book/chapter-1.html"
        private const val URL2 = "https://selftest.invalid/book/chapter-2.html"
        private const val URL3 = "https://selftest.invalid/book/chapter-3.html"
        const val BOOK_NAME = "自检之书"
        const val CHAPTER1_NAME = "第一章 起"
        const val CHAPTER2_NAME = "第二章 承"
        const val CHAPTER3_NAME = "第三章 转"
        const val CHAPTER1_BODY_1 = "第一段正文，用于验证原生阅读器渲染。"
        const val CHAPTER1_BODY_2 = "第二段正文，验证段落切分与进度计算。"
        const val FAILED_REASON = "自检：模拟抓取失败"

        fun cacheKeyOf(url: String): String =
            Regex("[^\\w\\u4e00-\\u9fff-]").replace(url, "_").takeLast(60)
    }

    private val ctx: Context = InstrumentationRegistry.getInstrumentation().targetContext
    private val runtimeDir = File(ctx.filesDir, "runtime")
    private val booksDir = File(runtimeDir, "books")
    private val bookDir = File(booksDir, key)
    private val progressFile = File(runtimeDir, "book_progress.json")
    private var savedProgress: ByteArray? = null
    private var savedProgressExisted = false

    val bookKey: String get() = key
    val detailUrl: String get() = "/api/books/$key"
    val chapter1Url: String get() = "/api/books/$key/chapter/1"
    val chapter2Url: String get() = "/api/books/$key/chapter/2"
    val progressUrl: String get() = "/api/books/$key/progress"

    /**
     * 造书 + 备份全局进度文件（先备份再动数据，异常也不会留下半截状态）。
     * 同时**清掉本用例键**的历史残留，保证每次运行都从确定状态开始——
     * 上一轮异常中断留下的进度不能影响本轮断言。
     */
    fun create() {
        // 先清掉本用例键（含历史中断残留），再把**清理后**的内容作为还原快照，
        // 否则 cleanup 会把上一轮的残留又还原回来（实测踩到：进度残留=1）
        removeProgressKey()
        if (progressFile.exists()) {
            savedProgress = progressFile.readBytes()
            savedProgressExisted = true
        } else {
            savedProgress = null
            savedProgressExisted = false
        }
        bookDir.deleteRecursively()
        require(bookDir.mkdirs()) { "无法创建自检书目录：$bookDir" }

        val state = JSONObject().apply {
            put("book", JSONObject().apply {
                put("name", BOOK_NAME)
                put("author", "自检")
                put("intro", "本目录由仪器化自检创建，用于验证详情页与原生阅读器的数据链路。")
                put("source_uid", "selftest")
                put("book_url", "https://selftest.invalid/book/")
            })
            put("chapters", JSONArray().apply {
                put(JSONObject().put("name", CHAPTER1_NAME).put("url", URL1))
                put(JSONObject().put("name", CHAPTER2_NAME).put("url", URL2))
                put(JSONObject().put("name", CHAPTER3_NAME).put("url", URL3))
            })
            put("completed", JSONArray().put(URL1))
            put("failed", JSONObject().put(URL3, FAILED_REASON))
            put("updated_at", "2026-01-01T00:00:00")
            put("total_chars", 0)
        }
        File(bookDir, "_state.json").writeText(state.toString(), Charsets.UTF_8)

        // 书目录下的 _progress.json 是**抓取任务进度**（不是阅读位置）。
        // 刻意写一个与阅读进度不同的值，用来锁住"两者不能被混用"。
        File(bookDir, "_progress.json").writeText(
            JSONObject().put("status", "paused").put("completed", 1).put("total", 3).toString(),
            Charsets.UTF_8,
        )

        // 第 1 章正文缓存：服务端从缓存读正文，cache 文件即"已下载"的判据
        File(bookDir, cacheKeyOf(URL1) + ".cache")
            .writeText("$CHAPTER1_NAME\n\n$CHAPTER1_BODY_1\n$CHAPTER1_BODY_2\n", Charsets.UTF_8)
    }

    /** 自检书是否仍在服务端的书库里（用于断言清理干净） */
    fun exists(): Boolean = bookDir.isDirectory && File(bookDir, "_state.json").isFile

    fun progressEntryCount(): Int {
        if (!progressFile.exists()) return 0
        val o = runCatching { JSONObject(progressFile.readText(Charsets.UTF_8)) }.getOrNull() ?: return 0
        return o.optJSONObject(key)?.let { 1 } ?: 0
    }

    /** 从全局进度文件里移除本用例的键（文件原本不存在且移除后为空 → 删除文件） */
    private fun removeProgressKey() {
        if (!progressFile.exists()) return
        val o = runCatching { JSONObject(progressFile.readText(Charsets.UTF_8)) }.getOrNull() ?: return
        if (!o.has(key)) return
        o.remove(key)
        if (o.length() == 0) progressFile.delete()
        else progressFile.writeText(o.toString(), Charsets.UTF_8)
    }

    /** 删书目录、恢复全局阅读进度文件（不留测试残留） */
    fun cleanup() {
        bookDir.deleteRecursively()
        File(booksDir, key).deleteRecursively()
        try {
            if (savedProgressExisted && savedProgress != null) {
                progressFile.writeBytes(savedProgress!!)
            } else if (progressFile.exists()) {
                progressFile.delete()
            }
            removeProgressKey()   // 双保险：还原后仍不得含本用例键
        } catch (_: Throwable) {
        }
        // 服务端软删除会移入 runtime/trash（0.55.0 修正：此前写成 data/trash，
        // 清理从未命中，设备上一直在攒垃圾）：清掉本次产生的条目
        File(runtimeDir, "trash").listFiles()
            ?.filter { it.name.startsWith(key) }
            ?.forEach { it.deleteRecursively() }
    }

    /** 断言用：把 base64 正文还原（日志里避免中文乱码） */
    fun b64(s: String): String =
        Base64.encodeToString(s.toByteArray(Charsets.UTF_8), Base64.NO_WRAP)
}
