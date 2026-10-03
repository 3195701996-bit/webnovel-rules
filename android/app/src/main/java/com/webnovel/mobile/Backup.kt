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
 *   manga/_favorites.json   漫画收藏与更新基准
 * 打包时同时写 manifest.json：格式版本、创建时间、文件大小与 SHA-256。
 *
 * 安全约束（恢复时必须守）：
 *   - 只接受白名单内的相对路径，拒绝绝对路径与 ".."（zip-slip）；
 *   - 逐文件校验 SHA-256，清单与内容不一致就整体失败，不做"部分恢复"；
 *   - 完整校验后才写入；提交前在私有磁盘保存新数据、不可变 journal 与旧文件快照，
 *     异常时回滚，进程被杀后由引擎启动前扫描恢复，避免一半新一半旧。
 *
 * 范围说明：书籍正文与漫画图片体积大且可重新下载，因此不在此备份范围内；
 * 界面对此必须明说，不能让用户以为"备份=整库离线可读"。
 */
object Backup {

    private data class StagedFile(
        val relativePath: String,
        val file: File,
        val size: Long,
        val sha256: String,
    )

    internal interface RestoreFileOps {
        fun exists(file: File): Boolean
        fun rename(from: File, to: File): Boolean
        fun delete(file: File): Boolean
        fun write(file: File, bytes: ByteArray)
        fun copy(from: File, to: File)
    }

    /** Roll back any restore transaction left by process death before the engine starts. */
    fun recoverInterruptedRestores(dataDir: File): List<String> {
        val issues = mutableListOf<String>()
        val transactions = dataDir.listFiles()
            ?.filter { it.isDirectory && it.name.startsWith(".restore-") }
            .orEmpty()
        for (transaction in transactions) {
            val journalFile = File(transaction, "journal.json")
            if (!journalFile.isFile) {
                // No journal means commit never began; live user data was untouched.
                transaction.deleteRecursively()
                continue
            }
            try {
                val committedMarker = File(transaction, "committed")
                if (committedMarker.isFile &&
                    committedMarker.readText(Charsets.UTF_8) == RESTORE_COMMITTED_MARKER) {
                    // The new data is authoritative. Cleanup may have been interrupted; never
                    // roll back a transaction whose durable commit marker was published.
                    transaction.deleteRecursively()
                    continue
                }
                val journal = JSONObject(journalFile.readText(Charsets.UTF_8))
                val entries = journal.optJSONArray("entries") ?: JSONArray()
                val failures = mutableListOf<String>()
                for (index in entries.length() - 1 downTo 0) {
                    val row = entries.optJSONObject(index) ?: continue
                    if (!File(transaction, "$index.started").isFile) continue
                    val relative = row.optString("path")
                    if (!isAllowedPath(relative)) {
                        failures += "$relative: invalid journal path"
                        continue
                    }
                    val target = File(dataDir, relative)
                    val oldFile = File(transaction, "$index.old")
                    try {
                        if (row.optBoolean("existed")) {
                            if (!oldFile.isFile) {
                                val expected = row.optString("old_sha256")
                                if (target.isFile && expected.isNotEmpty() &&
                                    sha256File(target) == expected) {
                                    continue // marker persisted, original rename not yet reached
                                }
                                throw IOException("原文件快照缺失且目标不匹配原始 SHA-256")
                            }
                            val expected = row.optString("old_sha256")
                            if (expected.isNotEmpty() && sha256File(oldFile) != expected) {
                                throw IOException("原文件快照 SHA-256 不匹配；保留现场供人工恢复")
                            }
                            val replacement = File(target.parentFile,
                                ".${target.name}.recovery-${java.util.UUID.randomUUID()}")
                            oldFile.inputStream().buffered().use { input ->
                                replacement.outputStream().buffered().use { output -> input.copyTo(output) }
                            }
                            if (target.exists() && !target.delete()) {
                                replacement.delete()
                                throw IOException("无法移除恢复中的文件")
                            }
                            if (!replacement.renameTo(target)) {
                                replacement.delete()
                                throw IOException("无法还原原文件")
                            }
                        } else if (target.exists() && !target.delete()) {
                            throw IOException("无法删除恢复时新建的文件")
                        }
                    } catch (t: Throwable) {
                        failures += "$relative: ${t.message ?: t.javaClass.simpleName}"
                    }
                }
                if (failures.isEmpty()) transaction.deleteRecursively()
                else issues += "${transaction.name}: ${failures.joinToString()}"
            } catch (t: Throwable) {
                issues += "${transaction.name}: journal recovery failed (${t.message ?: t.javaClass.simpleName})"
            }
        }
        return issues
    }

    private object SystemRestoreFileOps : RestoreFileOps {
        override fun exists(file: File) = file.exists()
        override fun rename(from: File, to: File) = from.renameTo(to)
        override fun delete(file: File) = !file.exists() || file.delete()
        override fun write(file: File, bytes: ByteArray) {
            file.outputStream().buffered().use { it.write(bytes) }
        }
        override fun copy(from: File, to: File) {
            from.inputStream().buffered().use { input ->
                to.outputStream().buffered().use { output -> input.copyTo(output) }
            }
        }
    }

    /** Commit a fully validated restore plan, retaining disk snapshots for rollback. */
    internal fun commitRestorePlan(
        plan: List<Pair<File, ByteArray>>,
        transactionDir: File,
        ops: RestoreFileOps = SystemRestoreFileOps,
    ) {
        if (!transactionDir.mkdirs()) throw IOException("无法创建恢复事务暂存目录")
        val staged = mutableListOf<Triple<File, File, File?>>()
        try {
            plan.forEachIndexed { index, (target, bytes) ->
                val stagedNew = File(transactionDir, "$index.new")
                ops.write(stagedNew, bytes)
                val oldSnapshot = if (ops.exists(target)) {
                    File(transactionDir, "$index.old")
                } else null
                staged += Triple(target, stagedNew, oldSnapshot)
            }

            val journal = JSONObject().put("version", 1).put("entries", JSONArray().apply {
                staged.forEach { (target, _, oldSnapshot) ->
                    put(JSONObject().put("path", target.relativeTo(transactionDir.parentFile!!)
                        .invariantSeparatorsPath)
                        .put("existed", oldSnapshot != null)
                        .put("old_sha256", if (oldSnapshot != null) sha256File(target) else ""))
                }
            })
            val journalFile = File(transactionDir, "journal.json")
            java.io.FileOutputStream(journalFile).use {
                it.write(journal.toString().toByteArray(Charsets.UTF_8))
                it.fd.sync()
            }
            var attempted = 0
            try {
                staged.forEachIndexed { index, (target, stagedNew, oldSnapshot) ->
                    target.parentFile?.let { parent ->
                        if (!parent.isDirectory && !parent.mkdirs()) {
                            throw IOException("无法创建恢复目录：${parent.name}")
                        }
                    }
                    val marker = File(transactionDir, "$index.started")
                    java.io.FileOutputStream(marker).use { it.fd.sync() }
                    attempted = index + 1
                    if (oldSnapshot != null && ops.exists(target) &&
                        !ops.rename(target, oldSnapshot)) {
                        throw IOException("无法保留原文件快照：${target.name}")
                    }
                    val replacement = File(target.parentFile,
                        ".${target.name}.restore-${java.util.UUID.randomUUID()}")
                    ops.copy(stagedNew, replacement)
                    if (!ops.rename(replacement, target)) {
                        ops.delete(replacement)
                        throw IOException("无法提交恢复文件：${target.name}")
                    }
                }
                val markerTemp = File(transactionDir, "committed.tmp")
                java.io.FileOutputStream(markerTemp).use {
                    it.write(RESTORE_COMMITTED_MARKER.toByteArray(Charsets.UTF_8))
                    it.fd.sync()
                }
                if (!markerTemp.renameTo(File(transactionDir, "committed"))) {
                    throw IOException("无法持久化恢复事务提交标记")
                }
            } catch (commitFailure: Throwable) {
                val rollbackFailures = mutableListOf<String>()
                staged.take(attempted).asReversed().forEach { (target, _, oldSnapshot) ->
                    try {
                        if (oldSnapshot == null) {
                            if (!ops.delete(target)) throw IOException("无法删除新建文件")
                        } else {
                            if (!ops.delete(target)) throw IOException("无法移除部分恢复文件")
                            target.parentFile?.mkdirs()
                            if (!ops.rename(oldSnapshot, target)) {
                                throw IOException("无法还原原文件")
                            }
                        }
                    } catch (rollbackFailure: Throwable) {
                        rollbackFailures += "${target.name}: ${rollbackFailure.message ?: rollbackFailure.javaClass.simpleName}"
                    }
                }
                if (rollbackFailures.isNotEmpty()) {
                    throw IOException(
                        "恢复提交失败，且部分回滚失败；原数据快照保留在 ${transactionDir.name}。" +
                            "失败文件：${rollbackFailures.joinToString()}", commitFailure)
                }
                throw IOException(
                    "恢复失败，已回滚：${commitFailure.message ?: commitFailure.javaClass.simpleName}",
                    commitFailure)
            }
        } catch (t: Throwable) {
            if (!t.message.orEmpty().contains("原数据快照保留")) {
                transactionDir.deleteRecursively()
            }
            throw t
        }
        transactionDir.deleteRecursively()
    }

    private const val TAG = "Backup"
    private const val RESTORE_COMMITTED_MARKER = "RESTORE_COMMITTED_V1"
    const val FORMAT = 1
    const val MANIFEST = "manifest.json"
    // Restore buffers validated payloads and a rollback copy before committing. Bound the
    // decompressed input, not only the ZIP's compressed size, to prevent zip bombs/OOM.
    private const val MAX_ARCHIVE_ENTRIES = 128
    private const val MAX_MANIFEST_BYTES = 2 * 1024 * 1024
    private const val MAX_FILE_BYTES = 16 * 1024 * 1024
    private const val MAX_TOTAL_BYTES = 64 * 1024 * 1024

    /** 白名单：相对数据目录的路径。目录项按前缀匹配 sources/ 下的 .json */
    private val FIXED_FILES = listOf(
        "book_progress.json", "manga/_library.json", "manga/_history.json", "manga/_favorites.json",
    )

    /** 备份范围（写进清单，也与界面/服务端 /api/backup/scope 同一口径） */
    private val INCLUDES = listOf(
        "sources/*.json（书源配置与启用状态）",
        "book_progress.json（小说阅读进度）",
        "manga/_library.json（漫画书库）",
        "manga/_history.json（漫画阅读历史）",
        "manga/_favorites.json（漫画收藏与更新基准）",
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

    private fun sha256File(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().buffered().use { input ->
            val buffer = ByteArray(8192)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

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
        if (files.size > MAX_ARCHIVE_ENTRIES - 1) {
            throw IOException("可备份文件过多（最多 ${MAX_ARCHIVE_ENTRIES - 1} 个文件）")
        }
        val srcCount = BundledSources.count(File(dir, "sources"))
        val warning = if (srcCount == 0) {
            "书源目录为空：备份里没有书源配置（可能 APK 未内置书源）"
        } else null
        val createdAt = now()
        val snapshotDir = File(ctx.cacheDir, "backup-snapshot-${java.util.UUID.randomUUID()}")
        if (!snapshotDir.mkdirs()) throw IOException("无法创建备份临时快照")
        try {
        // Read each live file exactly once into bounded app-cache staging. Both the manifest
        // digest and ZIP payload then come from the same byte snapshot, without holding the
        // complete user backup in memory while reading/writing it.
        var filesBytes = 0L
        val snapshots = ArrayList<StagedFile>(files.size)
        val copyBuffer = ByteArray(8192)
        files.forEachIndexed { index, source ->
            val staged = File(snapshotDir, "${index}.snapshot")
            val digest = MessageDigest.getInstance("SHA-256")
            var size = 0L
            source.inputStream().buffered().use { input ->
                staged.outputStream().buffered().use { output ->
                    while (true) {
                        val n = input.read(copyBuffer)
                        if (n < 0) break
                        size += n
                        filesBytes += n
                        if (size > MAX_FILE_BYTES) throw IOException("备份文件过大：${rel(dir, source)}")
                        if (filesBytes > MAX_TOTAL_BYTES) {
                            throw IOException("备份数据超过 ${MAX_TOTAL_BYTES / (1024 * 1024)} MiB 上限")
                        }
                        digest.update(copyBuffer, 0, n)
                        output.write(copyBuffer, 0, n)
                    }
                }
            }
            val hash = digest.digest().joinToString("") { "%02x".format(it) }
            snapshots += StagedFile(rel(dir, source), staged, size, hash)
        }
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
                snapshots.forEach { snapshot ->
                    put(JSONObject().apply {
                        put("path", snapshot.relativePath)
                        put("size", snapshot.size)
                        put("sha256", snapshot.sha256)
                    })
                }
            })
        }
        val manifestBytes = manifest.toString().toByteArray(Charsets.UTF_8)
        if (manifestBytes.size > MAX_MANIFEST_BYTES) throw IOException("备份清单过大")
        if (filesBytes + manifestBytes.size > MAX_TOTAL_BYTES) {
            throw IOException("备份数据超过 ${MAX_TOTAL_BYTES / (1024 * 1024)} MiB 上限")
        }
        var total = 0L
        val out = ctx.contentResolver.openOutputStream(uri, "w")
            ?: throw IOException("无法写入所选位置（系统未返回可写流）")
        ZipOutputStream(out.buffered()).use { zos ->
            fun write(name: String, bytes: ByteArray) {
                if (bytes.size > if (name == MANIFEST) MAX_MANIFEST_BYTES else MAX_FILE_BYTES) {
                    throw IOException("备份文件过大：$name")
                }
                if (total + bytes.size > MAX_TOTAL_BYTES) {
                    throw IOException("备份数据超过 ${MAX_TOTAL_BYTES / (1024 * 1024)} MiB 上限")
                }
                zos.putNextEntry(ZipEntry(name))
                zos.write(bytes)
                zos.closeEntry()
                total += bytes.size
            }
            write(MANIFEST, manifestBytes)
            snapshots.forEach { snapshot ->
                if (total + snapshot.size > MAX_TOTAL_BYTES) {
                    throw IOException("备份数据超过 ${MAX_TOTAL_BYTES / (1024 * 1024)} MiB 上限")
                }
                if (snapshot.file.length() != snapshot.size) {
                    throw IOException("备份临时快照大小异常：${snapshot.relativePath}")
                }
                zos.putNextEntry(ZipEntry(snapshot.relativePath))
                val stagedDigest = MessageDigest.getInstance("SHA-256")
                var copied = 0L
                snapshot.file.inputStream().buffered().use { input ->
                    while (true) {
                        val n = input.read(copyBuffer)
                        if (n < 0) break
                        if (total + n > MAX_TOTAL_BYTES) {
                            throw IOException("备份数据超过 ${MAX_TOTAL_BYTES / (1024 * 1024)} MiB 上限")
                        }
                        zos.write(copyBuffer, 0, n)
                        stagedDigest.update(copyBuffer, 0, n)
                        copied += n
                        total += n
                    }
                }
                zos.closeEntry()
                val actualHash = stagedDigest.digest().joinToString("") { "%02x".format(it) }
                if (actualHash != snapshot.sha256 || copied != snapshot.size) {
                    throw IOException("备份临时快照校验失败：${snapshot.relativePath}")
                }
            }
        }
        val report = BackupReport(files.size, total, createdAt, appVersion, warning)
        Log.i(TAG, "备份完成 文件=${report.files} 字节=${report.bytes} 书源=$srcCount")
        report
        } finally {
            snapshotDir.deleteRecursively()
        }
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
        val seenNames = HashSet<String>()
        var entryCount = 0
        var totalBytes = 0L
        val accountBytes: (Long) -> Unit = { count ->
            totalBytes += count
            if (totalBytes > MAX_TOTAL_BYTES) {
                throw IOException("备份解压后过大（最多 ${MAX_TOTAL_BYTES / (1024 * 1024)} MiB）")
            }
        }
        ctx.contentResolver.openInputStream(uri)?.use { ins ->
            ZipInputStream(ins.buffered()).use { zis ->
                var e: ZipEntry? = zis.nextEntry
                while (e != null) {
                    val name = e.name
                    entryCount++
                    if (entryCount > MAX_ARCHIVE_ENTRIES) {
                        throw IOException("备份条目过多（最多 $MAX_ARCHIVE_ENTRIES 项）")
                    }
                    if (!seenNames.add(name)) throw IOException("备份包含重复路径：$name")
                    if (name == MANIFEST) {
                        val bytes = readEntryBounded(zis, MAX_MANIFEST_BYTES, name, accountBytes)
                        manifest = runCatching { JSONObject(String(bytes, Charsets.UTF_8)) }.getOrNull()
                            ?: throw IOException("备份清单损坏（$MANIFEST 不是合法 JSON）")
                    } else if (!isAllowedPath(name)) {
                        // 先记下不合规路径并**丢弃其内容**（不占内存），
                        // 等读完再决定报错顺序：外来的 zip 更该先告诉用户"这不是本应用的备份"
                        if (firstBadPath == null) firstBadPath = name
                        drainEntryBounded(zis, accountBytes)
                    } else {
                        payload[name] = readEntryBounded(zis, MAX_FILE_BYTES, name, accountBytes)
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
        if (entries.length() > MAX_ARCHIVE_ENTRIES - 1) {
            throw IOException("备份清单条目过多（最多 ${MAX_ARCHIVE_ENTRIES - 1} 个文件）")
        }

        // 1) 先逐条校验（路径 + 完整性），全通过才动磁盘
        val plan = mutableListOf<Pair<File, ByteArray>>()
        var skipped = 0
        val declaredPaths = HashSet<String>()
        for (i in 0 until entries.length()) {
            val o = entries.optJSONObject(i) ?: throw IOException("备份清单第 ${i + 1} 项格式无效")
            val path = o.optString("path")
            if (!isAllowedPath(path)) throw IOException("清单里有不允许的路径：$path")
            if (!declaredPaths.add(path)) throw IOException("清单包含重复路径：$path")
            val bytes = payload[path]
            if (bytes == null) {
                skipped++
                continue
            }
            if (o.has("size") && o.optLong("size", -1L) != bytes.size.toLong()) {
                throw IOException("备份文件大小与清单不一致：$path")
            }
            val expect = o.optString("sha256")
            if (expect.isNotEmpty() && sha256(bytes) != expect) {
                throw IOException("备份内容校验失败（SHA-256 不一致）：$path")
            }
            plan.add(File(dir, path) to bytes)
        }
        if (plan.isEmpty()) throw IOException("备份里没有可恢复的文件")

        // 2) Stage both new payloads and rollback copies on disk before changing any live
        // file. This avoids retaining up to 128 MiB (old + new) in heap during restore.
        val transactionDir = File(dir, ".restore-${java.util.UUID.randomUUID()}")
        // 3) Commit only after the entire archive has passed validation.
        commitRestorePlan(plan, transactionDir)
        val report = RestoreReport(plan.size, skipped, m.optString("created_at"))
        Log.i(TAG, "恢复完成 写入=${report.restored} 跳过=${report.skipped}")
        report
    }

    /** Read one compressed entry while enforcing its decompressed size before allocation grows. */
    private fun readEntryBounded(
        zis: ZipInputStream,
        maxBytes: Int,
        name: String,
        onBytes: (Long) -> Unit,
    ): ByteArray {
        val out = java.io.ByteArrayOutputStream(minOf(maxBytes, 8192))
        val buf = ByteArray(8192)
        var size = 0
        while (true) {
            val n = zis.read(buf)
            if (n < 0) break
            size += n
            onBytes(n.toLong())
            if (size > maxBytes) throw IOException("备份文件过大：$name")
            out.write(buf, 0, n)
        }
        return out.toByteArray()
    }

    /** Drain a rejected path without retaining it, while still charging it to the archive budget. */
    private fun drainEntryBounded(zis: ZipInputStream, onBytes: (Long) -> Unit) {
        val buf = ByteArray(8192)
        while (true) {
            val n = zis.read(buf)
            if (n < 0) break
            onBytes(n.toLong())
        }
    }

    /** 备份文件名建议：webnovel-backup-YYYYMMDD-HHmm.zip */
    fun suggestName(): String {
        val ts = java.text.SimpleDateFormat("yyyyMMdd-HHmm", java.util.Locale.US).format(java.util.Date())
        return "webnovel-backup-$ts.zip"
    }
}
