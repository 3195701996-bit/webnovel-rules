package com.webnovel.mobile

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File

/**
 * 自检用「合成漫画」：在应用私有目录里按**真实的漫画目录结构**造一部三话的漫画，
 * 让漫画阅读链路的验收不依赖外部站点与网络：
 *
 *   源目录   manga/downloads/<source>/<comic_id>/
 *   话数据   .../<chapter_id>/0000.jpg（服务端判据：目录内有图片文件 = 已下载，
 *            且单张 > 1000 字节才会被本地直出）
 *   元数据   .../_info.json（编辑用章节列表；详情接口的"本地快速路径"读它）
 *   书库     manga/_library.json（书库列表来源）
 *   历史     manga/_history.json（阅读位置，与网页端共用）
 *
 * 三话刻意分成三种状态：第 1/2 话已下载（各 2/1 张图），第 3 话未下载。
 * source 用已注册的 mangadex（章节目录接口会先查适配器是否存在），
 * 但所有断言的路径都命中本地文件，不产生任何源站请求。
 */
class SelfTestComic(
    private val source: String = "mangadex",
    private val comicId: String = "__selftest__",
) {
    companion object {
        const val TITLE = "自检漫画"
        const val CH1_ID = "ch-1"
        const val CH2_ID = "ch-2"
        const val CH3_ID = "ch-3"
        const val CH1_NAME = "第 1 话 起"
        const val CH2_NAME = "第 2 话 承"
        const val CH3_NAME = "第 3 话 转"
        const val GROUP_A = "第一卷"
        const val GROUP_B = "第二卷"

        /**
         * 服务端要求图片 > 1000 字节才本地直出，故画一张有内容的 JPEG。
         *
         * 刻意用**细长比例**（240×600，高/宽=2.5）：如果阅读器写死宽高比（例如 0.7），
         * 长图会被裁切，界面用例会按实际渲染比例断言并失败。
         */
        fun makeJpeg(seed: Int, w: Int = 240, h: Int = 600): ByteArray {
            val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
            val c = Canvas(bmp)
            val p = Paint()
            for (y in 0 until h step 4) {
                val t = y.toFloat() / h
                p.color = Color.rgb(
                    (40 + seed * 20 + t * 120).toInt().coerceIn(0, 255),
                    (70 + t * 100).toInt().coerceIn(0, 255),
                    (140 - seed * 15 + t * 80).toInt().coerceIn(0, 255),
                )
                c.drawRect(0f, y.toFloat(), w.toFloat(), (y + 4).toFloat(), p)
            }
            p.color = Color.WHITE
            p.textSize = 28f
            c.drawText("selftest p$seed", 16f, h / 2f, p)
            // 画满整个高度，确保细长页面上下都有内容（便于肉眼确认没有被裁）
            c.drawRect(0f, h - 6f, w.toFloat(), h.toFloat(), p)
            val bos = ByteArrayOutputStream()
            bmp.compress(Bitmap.CompressFormat.JPEG, 92, bos)
            bmp.recycle()
            return bos.toByteArray()
        }
    }

    private val ctx: Context = InstrumentationRegistry.getInstrumentation().targetContext
    private val mangaDir = File(ctx.filesDir, "runtime/manga")
    private val comicDir = File(mangaDir, "downloads/$source/$comicId")
    private val libraryFile = File(mangaDir, "_library.json")
    private val historyFile = File(mangaDir, "_history.json")
    private var savedLibrary: ByteArray? = null
    private var savedLibraryExisted = false
    private var savedHistory: ByteArray? = null
    private var savedHistoryExisted = false

    val sourceKey: String get() = source
    val comicIdValue: String get() = comicId
    val detailUrl: String get() = "/api/manga/$source/$comicId"
    fun chapterUrl(chapterId: String) = "/api/manga/$source/$comicId/chapter/$chapterId"
    fun imageUrl(chapterId: String, idx: Int) = chapterUrl(chapterId) + "/img/$idx"

    /** 先清掉本用例残留，再把清理后的状态作为还原快照 */
    fun create() {
        removeLibraryEntry()
        removeHistoryEntry()
        savedLibrary = if (libraryFile.exists()) libraryFile.readBytes().also { savedLibraryExisted = true } else null
        savedHistory = if (historyFile.exists()) historyFile.readBytes().also { savedHistoryExisted = true } else null

        comicDir.deleteRecursively()
        require(comicDir.mkdirs()) { "无法创建自检漫画目录：$comicDir" }

        // 第 1 话：2 张图（已下载）
        writePage(CH1_ID, 0, 0)
        writePage(CH1_ID, 1, 1)
        // 第 2 话：1 张图（已下载）
        writePage(CH2_ID, 0, 2)
        // 第 3 话：只建 URL 缓存、不放图片 → 未下载（与真实"仅解析未下载"一致）

        // _info.json：详情接口的本地快速路径
        val info = JSONObject().apply {
            put("title", TITLE)
            put("cover", "")
            put("comic_id", comicId)
            put("source", source)
            put("chapters", JSONArray().apply {
                put(JSONObject().put("id", CH1_ID).put("name", CH1_NAME).put("group", GROUP_A))
                put(JSONObject().put("id", CH2_ID).put("name", CH2_NAME).put("group", GROUP_A))
                put(JSONObject().put("id", CH3_ID).put("name", CH3_NAME).put("group", GROUP_B))
            })
        }
        File(comicDir, "_info.json").writeText(info.toString(), Charsets.UTF_8)

        // 书库条目
        val lib = if (libraryFile.exists()) {
            runCatching { JSONArray(libraryFile.readText(Charsets.UTF_8)) }.getOrNull() ?: JSONArray()
        } else JSONArray()
        lib.put(JSONObject().apply {
            put("source", source)
            put("comic_id", comicId)
            put("title", TITLE)
            put("cover", "")
            put("status", "done")
            put("images", 3)
            put("chapters", 3)
        })
        libraryFile.parentFile?.mkdirs()
        libraryFile.writeText(lib.toString(), Charsets.UTF_8)
    }

    private fun writePage(chapterId: String, idx: Int, seed: Int) {
        val d = File(comicDir, chapterId)
        require(d.mkdirs() || d.isDirectory) { "无法创建话目录：$d" }
        File(d, String.format("%04d.jpg", idx)).writeBytes(makeJpeg(seed))
    }

    fun imageSize(chapterId: String, idx: Int): Long =
        File(comicDir, chapterId + "/" + String.format("%04d.jpg", idx)).length()

    private fun readJsonArray(f: File): JSONArray =
        if (f.exists()) runCatching { JSONArray(f.readText(Charsets.UTF_8)) }.getOrNull()
            ?: JSONArray() else JSONArray()

    private fun readJsonObject(f: File): JSONObject =
        if (f.exists()) runCatching { JSONObject(f.readText(Charsets.UTF_8)) }.getOrNull()
            ?: JSONObject() else JSONObject()

    private fun writeArrayKeepingKeys(f: File, arr: JSONArray) {
        if (arr.length() == 0) {
            f.delete()
        } else {
            f.writeText(arr.toString(), Charsets.UTF_8)
        }
    }

    private fun removeLibraryEntry() {
        val arr = readJsonArray(libraryFile)
        val out = JSONArray()
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            if (o.optString("source") == source && o.optString("comic_id") == comicId) continue
            out.put(o)
        }
        writeArrayKeepingKeys(libraryFile, out)
    }

    private fun removeHistoryEntry() {
        val o = readJsonObject(historyFile)
        if (!o.has("$source:$comicId")) return
        o.remove("$source:$comicId")
        if (o.length() == 0) historyFile.delete() else historyFile.writeText(o.toString(), Charsets.UTF_8)
    }

    fun libraryEntryCount(): Int {
        val arr = readJsonArray(libraryFile)
        var n = 0
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            if (o.optString("source") == source && o.optString("comic_id") == comicId) n++
        }
        return n
    }

    fun historyEntryCount(): Int = if (readJsonObject(historyFile).has("$source:$comicId")) 1 else 0

    fun exists(): Boolean = File(comicDir, "_info.json").isFile

    /** 删数据目录、还原书库与历史文件（不留测试残留） */
    fun cleanup() {
        comicDir.deleteRecursively()
        File(mangaDir, "downloads/$source").takeIf { it.listFiles()?.isEmpty() == true }?.delete()
        File(mangaDir, "_cache/$source/$comicId").deleteRecursively()
        try {
            if (savedLibraryExisted && savedLibrary != null) libraryFile.writeBytes(savedLibrary!!)
            else if (libraryFile.exists()) libraryFile.delete()
            if (savedHistoryExisted && savedHistory != null) historyFile.writeBytes(savedHistory!!)
            else if (historyFile.exists()) historyFile.delete()
        } catch (_: Throwable) {
        }
        // 双保险：还原后仍不得含本用例条目
        removeLibraryEntry()
        removeHistoryEntry()
    }
}
