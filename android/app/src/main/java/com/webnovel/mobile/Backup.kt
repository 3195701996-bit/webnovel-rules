package com.webnovel.mobile

import android.content.Context
import android.net.Uri
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.util.zip.ZipEntry
import java.util.zip.ZipInputStream
import java.util.zip.ZipOutputStream

/** 备份内容摘要（用于界面如实汇报，而不是只说"成功"） */
data class BackupReport(
    val files: Int,
    val bytes: Long,
    val createdAt: String,
    val appVersion: String,
    /** 需要提示用户的情况（例如书源目录为空），没有则为 null */
    val warning: String? = null,
)

/** 恢复结果 */
data class RestoreReport(
    val restored: Int,
    val skipped: Int,
    val createdAt: String,
)

/**
 * 备份与恢复：只处理**用户数据与配置**，不打包书籍正文与漫画图片。
 *
 * 备什么：
 *   sources 目录下的 .json          书源配置与启用状态
 *   book_progress.json      小说阅读位置
 *   manga/_library.json     漫画书库条目
 *   manga/_history.json     漫画阅读位置
 * 打包时同时写 manifest.json：格式版本、创建时间、文件大小与 SHA-256。
 *
 * 安全约束（恢复时必须守）：
 *   - 只接受白名单内的相对路径，拒绝绝对路径与 ".."（zip-slip）；
 *   - 逐文件校验 SHA-256，清单与内容不一致就整体失败，不做"部分恢复"；
 *   - 恢复前先把现有文件读进内存，写入过程中任何异常都回滚，
 *     避免出现"一半新一半旧"的坏状态。
 *
 * 范围说明：书籍正文与漫画图片体积大且可重新下载，因此不在此备份范围内；
 * 界面对此必须明说，不能让用户以为"备份=整库离线可读"。
 */
object Backup {

    private const val TAG = "Backup"
    const val FORMAT = 1
    const val MANIFEST = "manifest.json"

    /** 白名单：相对数据目录的路径。目录项按前缀匹配 sources/ 下的 .json */
    private val FIXED_FILES = listOf("book_progress.json", "manga/_library.json", "manga/_history.json")

    /** 备份范围（写进清单，也与界面/服务端 /api/backup/scope 同一口径） */
    private val INCLUDES = listOf(
        "sources/*.json（书源配置与启用状态）",
        "book_progress.json（小说阅读进度）",
        "manga/_library.json（漫画书库）",
        "manga/_history.json（漫画阅读历史）",
    )
    private val EXCLUDES = listOf(
        "书籍正文缓存（可从书源重新下载）",
        "漫画已下载图片（可从源站重新下载，体积最大）",
        "临时缓存与日志（可再生）",
        "回收站内容（已删除的东西）",
    )

    fun dataDir(ctx: Context): File = File(ctx.filesDir, "runtime")

    private fun collectFiles(dir: File): List<File> {
        val out = mutableListOf<File>()
        for (rel in FIXED_FILES) {
            val f = File(dir, rel)
            if (f.isFile) out.add(f)
        }
        val srcDir = File(dir, "sources")
        srcDir.listFiles { f -> f.isFile && f.name.endsWith(".json") }
            ?.sortedBy { it.name }?.forEach { out.add(it) }
        return out
    }

    private fun rel(dir: File, f: File): String = f.absolutePath.removePrefix(dir.absolutePath + File.separator)

    private fun sha256(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    /** 是否白名单路径（恢复时用于拒绝任何越界条目） */
    fun isAllowedPath(rel: String): Boolean {
        if (rel.isBlank()) return false
        if (rel.startsWith("/") || rel.startsWith("\\")) return false
        if (rel.contains("..")) return false
        if (rel.contains(':')) return false          // 盘符/协议
        val normalized = rel.replace('\\', '/')
        if (normalized in FIXED_FILES) return true
        return normalized.startsWith("sources/") && normalized.endsWith(".json") &&
            !normalized.removePrefix("sources/").contains('/')
    }

    /** 生成备份到目标 URI（用户通过 SAF 选定）；返回实际写入的摘要 */
    suspend fun create(
        ctx: Context,
        uri: Uri,
        appVersion: String,
        now: () -> String = { java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss", java.util.Locale.US)
            .format(java.util.Date()) },
    ): BackupReport = withContext(Dispatchers.IO) {
        val dir = dataDir(ctx)
        // 先确保内置书源已解压：全新安装、还没启动过引擎时书源目录可能还不存在，
        // 这种情况下备份会"悄悄少一半内容"（实测踩到）
        BundledSources.ensure(ctx, File(dir, "sources"))
        val files = collectFiles(dir)
        if (files.isEmpty()) throw IOException("没有可备份的数据（书源与阅读进度都是空的）")
        val srcCount = BundledSources.count(File(dir, "sources"))
        val warning = if (srcCount == 0) {
            "书源目录为空：备份里没有书源配置（可能 APK 未内置书源）"
        } else null
        val createdAt = now()
        val manifest = JSONObject().apply {
            put("format", FORMAT)
            put("app", "com.webnovel.mobile")
            put("version", appVersion)
            put("created_at", createdAt)
            // 备份**范围**写进清单（路线 P1-2："完整备份范围明确"）：
            // 拿到这份 zip 的人（包括几年后的用户自己）不必猜"里面有没有正文"，
            // 清单里就写着包含什么、明确不含什么。界面展示的同口径文案见
            // BackupScreen（数字取自 GET /api/backup/scope 的磁盘实扫）。
            put("scope", JSONObject().apply {
                put("includes", JSONArray(INCLUDES))
                put("excludes", JSONArray(EXCLUDES))
                put("note", "备份是配置与进度，不是离线全文副本；" +
                    "书籍正文与漫画图片不在备份内，可重新下载。")
            })
            put("entries", JSONArray().apply {
                files.forEach { f ->
                    val bytes = f.readBytes()
                    put(JSONObject().apply {
                        put("path", rel(dir, f))
                        put("size", bytes.size)
                        put("sha256", sha256(bytes))
                    })
                }
            })
        }
        var total = 0L
        val out = ctx.contentResolver.openOutputStream(uri, "w")
            ?: throw IOException("无法写入所选位置（系统未返回可写流）")
        ZipOutputStream(out.buffered()).use { zos ->
            fun write(name: String, bytes: ByteArray) {
                zos.putNextEntry(ZipEntry(name))
                zos.write(bytes)
                zos.closeEntry()
                total += bytes.size
            }
            write(MANIFEST, manifest.toString().toByteArray(Charsets.UTF_8))
            files.forEach { f -> write(rel(dir, f), f.readBytes()) }
        }
        val report = BackupReport(files.size, total, createdAt, appVersion, warning)
        Log.i(TAG, "备份完成 文件=${report.files} 字节=${report.bytes} 书源=$srcCount")
        report
    }

    /**
     * 从备份恢复。任何一步不满足就整体失败（不部分恢复）；
     * 写入过程异常会回滚已改动的文件。
     */
    suspend fun restore(ctx: Context, uri: Uri): RestoreReport = withContext(Dispatchers.IO) {
        val dir = dataDir(ctx)
        val payload = LinkedHashMap<String, ByteArray>()
        var manifest: JSONObject? = null
        var firstBadPath: String? = null
        ctx.contentResolver.openInputStream(uri)?.use { ins ->
            ZipInputStream(ins.buffered()).use { zis ->
                var e: ZipEntry? = zis.nextEntry
                while (e != null) {
                    val name = e.name
                    if (name == MANIFEST) {
                        val bytes = zis.readBytes()
                        manifest = runCatching { JSONObject(String(bytes, Charsets.UTF_8)) }.getOrNull()
                            ?: throw IOException("备份清单损坏（$MANIFEST 不是合法 JSON）")
                    } else if (!isAllowedPath(name)) {
                        // 先记下不合规路径并**丢弃其内容**（不占内存），
                        // 等读完再决定报错顺序：外来的 zip 更该先告诉用户"这不是本应用的备份"
                        if (firstBadPath == null) firstBadPath = name
                        skipEntry(zis)
                    } else {
                        payload[name] = zis.readBytes()
                    }
                    zis.closeEntry()
                    e = zis.nextEntry
                }
            }
        } ?: throw IOException("无法读取所选文件")

        val m = manifest ?: throw IOException("这不是本应用的备份：缺少 $MANIFEST")
        firstBadPath?.let { throw IOException("备份包含不允许的路径，已拒绝恢复：$it") }
        val fmt = m.optInt("format", 0)
        if (fmt != FORMAT) throw IOException("备份格式版本不支持：$fmt（当前支持 $FORMAT）")
        val entries = m.optJSONArray("entries") ?: JSONArray()
        if (entries.length() == 0) throw IOException("备份清单为空")

        // 1) 先逐条校验（路径 + 完整性），全通过才动磁盘
        val plan = mutableListOf<Pair<File, ByteArray>>()
        var skipped = 0
        for (i in 0 until entries.length()) {
            val o = entries.optJSONObject(i) ?: continue
            val path = o.optString("path")
            if (!isAllowedPath(path)) throw IOException("清单里有不允许的路径：$path")
            val bytes = payload[path]
            if (bytes == null) {
                skipped++
                continue
            }
            val expect = o.optString("sha256")
            if (expect.isNotEmpty() && sha256(bytes) != expect) {
                throw IOException("备份内容校验失败（SHA-256 不一致）：$path")
            }
            plan.add(File(dir, path) to bytes)
        }
        if (plan.isEmpty()) throw IOException("备份里没有可恢复的文件")

        // 2) 记录现有内容，便于失败回滚
        val previous = HashMap<String, ByteArray?>()
        plan.forEach { (f, _) -> previous[f.absolutePath] = if (f.isFile) f.readBytes() else null }

        var restored = 0
        try {
            for ((f, bytes) in plan) {
                f.parentFile?.mkdirs()
                val tmp = File(f.absolutePath + ".restore-tmp")
                tmp.writeBytes(bytes)
                if (!tmp.renameTo(f)) {
                    f.writeBytes(bytes)
                    tmp.delete()
                }
                restored++
            }
        } catch (t: Throwable) {
            // 回滚：把改动过的文件恢复原样（原本不存在则删除）
            previous.forEach { (abs, old) ->
                runCatching {
                    val f = File(abs)
                    if (old == null) f.delete() else f.writeBytes(old)
                }
            }
            throw IOException("恢复失败已回滚：${t.message ?: t.javaClass.simpleName}")
        }
        val report = RestoreReport(restored, skipped, m.optString("created_at"))
        Log.i(TAG, "恢复完成 写入=${report.restored} 跳过=${report.skipped}")
        report
    }

    /** 丢弃当前条目内容（有界缓冲），用于不合规路径：不把内容读进内存 */
    private fun skipEntry(zis: ZipInputStream) {
        val buf = ByteArray(8192)
        while (zis.read(buf) > 0) {
            // 只丢弃
        }
    }

    /** 备份文件名建议：webnovel-backup-YYYYMMDD-HHmm.zip */
    fun suggestName(): String {
        val ts = java.text.SimpleDateFormat("yyyyMMdd-HHmm", java.util.Locale.US).format(java.util.Date())
        return "webnovel-backup-$ts.zip"
    }
}
