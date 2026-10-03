package com.webnovel.mobile

import android.net.Uri
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/**
 * 备份与恢复验收（离线、不触发网络）：
 *
 *   - 备份内容 = 书源配置 + 小说阅读进度 + 漫画书库/阅读进度/收藏（不含正文/图片）；
 *   - 清单（manifest）带格式版本、创建时间与逐文件 SHA-256；
 *   - 恢复要求整份备份通过校验才写入，被改动过的内容必须被识别（SHA-256 不一致即失败）；
 *   - **zip-slip**：含 "../xxx" 或绝对路径的备份必须整体拒绝，不写数据目录之外的任何文件；
 *   - 非本应用的 zip（没有 manifest）必须明确失败。
 *
 * 自检数据由 SelfTestBook / SelfTestComic 提供，结束全部清理。
 */
@RunWith(AndroidJUnit4::class)
class BackupPathTest {

    private lateinit var book: SelfTestBook
    private lateinit var comic: SelfTestComic
    private val ctx = InstrumentationRegistry.getInstrumentation().targetContext
    private val dataDir = File(ctx.filesDir, "runtime")
    private var backupFavoriteKey: String? = null
    private val testArtifacts = mutableListOf<File>()
    private val ownedFiles = mutableListOf<File>()

    private fun ev(line: String) = println("BACKUP_PATH_EVIDENCE $line")

    private fun artifact(name: String): File = File(
        ctx.cacheDir,
        "$name-${java.util.UUID.randomUUID()}.zip",
    ).also(testArtifacts::add)

    private fun ownedFile(name: String): File = File(
        ctx.filesDir,
        "$name-${java.util.UUID.randomUUID()}",
    ).also(ownedFiles::add)

    @Before
    fun setUp() {
        book = SelfTestBook(key = "selftest_backup_${java.util.UUID.randomUUID().toString().replace("-", "")}")
        book.create()
        try {
            comic = SelfTestComic()
            comic.create()
        } catch (t: Throwable) {
            runCatching { book.cleanupFixtureOnly() }
            throw t
        }
    }

    @After
    fun tearDown() {
        try {
            if (::book.isInitialized) book.cleanupFixtureOnly()
        } finally {
            try {
                if (::comic.isInitialized) runCatching { comic.cleanupFixtureOnly() }
            } finally {
                backupFavoriteKey?.let { key ->
                    val favoritesFile = File(dataDir, "manga/_favorites.json")
                    if (favoritesFile.isFile) runCatching {
                        val favorites = JSONObject(favoritesFile.readText(Charsets.UTF_8))
                        favorites.remove(key)
                        if (favorites.length() == 0) favoritesFile.delete()
                        else favoritesFile.writeText(favorites.toString(), Charsets.UTF_8)
                    }
                }
                testArtifacts.forEach(File::delete)
                ownedFiles.forEach(File::delete)
            }
        }
    }

    @Test
    fun backupThenMutateThenRestore() {
        val libFile = File(dataDir, "manga/_library.json")
        val histFile = File(dataDir, "manga/_history.json")
        val favoritesFile = File(dataDir, "manga/_favorites.json")
        val favoriteKey = "mangadex:selftest-favorite-${java.util.UUID.randomUUID()}"
        backupFavoriteKey = favoriteKey
        favoritesFile.parentFile?.mkdirs()
        val favorites = if (favoritesFile.isFile) JSONObject(favoritesFile.readText(Charsets.UTF_8)) else JSONObject()
        favorites.put(favoriteKey, JSONObject().put("title", "备份收藏自检")
            .put("read_chapter_ids", JSONArray().put("chapter-1")))
        favoritesFile.writeText(favorites.toString(), Charsets.UTF_8)

        // 0) 先写一条阅读进度，确保备份里有真实内容
        val progressFile = File(dataDir, "book_progress.json")
        val progress = if (progressFile.isFile) JSONObject(progressFile.readText(Charsets.UTF_8)) else JSONObject()
        progress.put(book.bookKey, JSONObject().apply {
            put("idx", 1); put("pct", 25); put("name", SelfTestBook.CHAPTER1_NAME)
            put("ts", 1_700_000_000)
        })
        progressFile.parentFile?.mkdirs()
        progressFile.writeText(progress.toString(), Charsets.UTF_8)

        val zip = artifact("selftest-backup")
        zip.delete()
        val snapshotsBefore = ctx.cacheDir.listFiles()
            ?.filter { it.name.startsWith("backup-snapshot-") }?.map { it.name }?.toSet().orEmpty()

        // 1) 备份
        val report = kotlinx.coroutines.runBlocking {
            Backup.create(ctx, Uri.fromFile(zip), appVersion = "selftest")
        }
        assertTrue("备份应有内容：${report.files} 个文件 / ${report.bytes} 字节",
            report.files > 0 && report.bytes > 0)
        assertTrue("备份文件应存在", zip.isFile)
        val entries = readZip(zip)
        assertTrue("备份应含清单", entries.containsKey(Backup.MANIFEST))
        assertTrue("备份应含阅读进度", entries.containsKey("book_progress.json"))
        assertTrue("备份应含漫画收藏", entries.containsKey("manga/_favorites.json"))
        // 全新安装（尚未启动过引擎）时书源目录可能还是空的——备份必须先解压内置书源，
        // 否则备份会悄悄缺一半内容（实测在 pm clear 之后的首次运行里踩到）
        assertTrue("备份应含书源目录下的文件（即使全新安装未启动过引擎）",
            entries.keys.any { it.startsWith("sources/") && it.endsWith(".json") })
        assertNull("书源齐全时不应产生告警", report.warning)
        val snapshotsAfter = ctx.cacheDir.listFiles()
            ?.filter { it.name.startsWith("backup-snapshot-") }?.map { it.name }?.toSet().orEmpty()
        assertEquals("备份完成后必须清理本次临时快照", snapshotsBefore, snapshotsAfter)
        val srcInZip = entries.keys.count { it.startsWith("sources/") && it.endsWith(".json") }
        assertEquals("备份里的书源数量应与磁盘一致",
            BundledSources.count(File(dataDir, "sources")), srcInZip)
        val manifest = JSONObject(String(entries[Backup.MANIFEST]!!, Charsets.UTF_8))
        assertEquals("格式版本", Backup.FORMAT, manifest.optInt("format"))
        assertTrue("清单应带创建时间", manifest.optString("created_at").isNotBlank())
        val manifestEntries = manifest.optJSONArray("entries")!!
        assertTrue("清单条目应与文件数一致", manifestEntries.length() >= report.files)
        ev("备份：文件=${report.files} 字节=${report.bytes} 清单条目=${manifestEntries.length()}")

        val progressSha = sha256(entries["book_progress.json"]!!)
        val favoritesSha = sha256(entries["manga/_favorites.json"]!!)

        // 2) 改动现场：删掉书、改掉进度
        File(dataDir, "books/${book.bookKey}").deleteRecursively()
        assertFalse("先确认书目录已删", File(dataDir, "books/${book.bookKey}").isDirectory)
        val changedProgress = JSONObject(progressFile.readText(Charsets.UTF_8)).apply { remove(book.bookKey) }
        progressFile.writeText(changedProgress.toString(), Charsets.UTF_8)
        val changedFavorites = JSONObject(favoritesFile.readText(Charsets.UTF_8)).apply { remove(favoriteKey) }
        favoritesFile.writeText(changedFavorites.toString(), Charsets.UTF_8)
        assertFalse("先确认本用例进度已被移除", JSONObject(progressFile.readText(Charsets.UTF_8)).has(book.bookKey))
        assertFalse("先确认本用例收藏已被移除", JSONObject(favoritesFile.readText(Charsets.UTF_8)).has(favoriteKey))

        // 3) 恢复
        val rr = kotlinx.coroutines.runBlocking { Backup.restore(ctx, Uri.fromFile(zip)) }
        assertTrue("恢复应至少写入 1 个文件", rr.restored > 0)
        assertEquals("恢复时间应来自清单", manifest.optString("created_at"), rr.createdAt)
        assertEquals("恢复后的进度内容应与备份一致", progressSha,
            sha256(progressFile.readBytes()))
        assertEquals("恢复后的漫画收藏应与备份一致", favoritesSha,
            sha256(favoritesFile.readBytes()))
        ev("恢复：写入=${rr.restored} 跳过=${rr.skipped} 进度/收藏已还原")

        // 书源目录应被还原（备份里有的都回来了）
        val srcNames = entries.keys.filter { it.startsWith("sources/") }
        assertTrue("备份中的书源文件都应还原",
            srcNames.all { File(dataDir, it).isFile })
        ev("恢复后书源文件齐全：${srcNames.size} 个")

        // 4) 被篡改的备份必须整体失败（SHA-256 校验）
        val tampered = artifact("selftest-backup-tampered")
        // 保持条目字节数不变，确保命中摘要校验而不是更早的 size 校验。
        val tamperedProgress = entries["book_progress.json"]!!.copyOf().apply {
            if (isNotEmpty()) this[0] = (this[0].toInt() xor 1).toByte()
        }
        writeZip(tampered, entries.toMutableMap().apply {
            this["book_progress.json"] = tamperedProgress
        })
        var tamperErr: Throwable? = null
        try {
            kotlinx.coroutines.runBlocking { Backup.restore(ctx, Uri.fromFile(tampered)) }
        } catch (t: Throwable) { tamperErr = t }
        assertTrue("篡改过的备份应失败", tamperErr != null)
        assertTrue("失败原因应指出校验问题，实际=${tamperErr?.message}",
            (tamperErr?.message ?: "").contains("SHA-256"))
        // 单文件 entries 长度不变，但校验必须拦下来；现有数据不应被这次失败影响
        assertEquals("校验失败不应改动现有数据", progressSha, sha256(progressFile.readBytes()))
        ev("篡改校验：${tamperErr?.message?.take(40)}")

        // 5) zip-slip：含 ../ 条目的备份必须整体拒绝，且不能写出数据目录
        val evilTarget = ownedFile("evil-escaped.json")
        val evilTargetName = evilTarget.name
        val evil = artifact("selftest-backup-evilslip")
        val evilManifest = JSONObject().apply {
            put("format", Backup.FORMAT)
            put("created_at", "2026-01-01 00:00:00")
            put("entries", JSONArray().put(JSONObject().apply {
                put("path", "../$evilTargetName")
                put("size", 5)
                put("sha256", sha256("evil!".toByteArray()))
            }))
        }
        writeZip(evil, linkedMapOf(
            Backup.MANIFEST to evilManifest.toString().toByteArray(Charsets.UTF_8),
            "../$evilTargetName" to "evil!".toByteArray(Charsets.UTF_8),
        ))
        var slipErr: Throwable? = null
        try {
            kotlinx.coroutines.runBlocking { Backup.restore(ctx, Uri.fromFile(evil)) }
        } catch (t: Throwable) { slipErr = t }
        assertTrue("zip-slip 备份应被拒绝", slipErr != null)
        assertFalse("绝不能写出数据目录之外的文件", evilTarget.exists())
        ev("zip-slip 被拒：${slipErr?.message?.take(50)}")

        // 6) 不是本应用的 zip（没有清单）必须明确失败
        val noManifest = artifact("selftest-backup-nomanifest")
        writeZip(noManifest, linkedMapOf("hello.txt" to "hi".toByteArray()))
        var nmErr: Throwable? = null
        try {
            kotlinx.coroutines.runBlocking { Backup.restore(ctx, Uri.fromFile(noManifest)) }
        } catch (t: Throwable) { nmErr = t }
        assertTrue("缺少清单应失败", nmErr != null)
        assertTrue("失败原因应说明缺少清单，实际=${nmErr?.message}",
            (nmErr?.message ?: "").contains(Backup.MANIFEST))
        ev("无清单备份被拒：${nmErr?.message?.take(40)}")

        // 7) 路径白名单：正常路径允许，越界与怪路径一律拒绝
        assertTrue(Backup.isAllowedPath("book_progress.json"))
        assertTrue(Backup.isAllowedPath("manga/_favorites.json"))
        assertTrue(Backup.isAllowedPath("sources/abc.json"))
        assertFalse(Backup.isAllowedPath("../x.json"))
        assertFalse(Backup.isAllowedPath("/etc/passwd"))
        assertFalse(Backup.isAllowedPath("sources/../x.json"))
        assertFalse(Backup.isAllowedPath("manga/evil.exe"))
        assertFalse(Backup.isAllowedPath("sources/sub/dir.json"))
        ev("路径白名单检查通过")

        // 8) 用户数据文件保持可用（备份/恢复不破坏当前运行状态）
        assertTrue("书库文件应仍可解析", !libFile.exists() ||
            runCatching { JSONArray(libFile.readText(Charsets.UTF_8)) }.isSuccess)
        assertTrue("历史文件应仍可解析", !histFile.exists() ||
            runCatching { JSONObject(histFile.readText(Charsets.UTF_8)) }.isSuccess)
        assertTrue("进度文件仍是合法 JSON",
            runCatching { JSONObject(progressFile.readText(Charsets.UTF_8)) }.isSuccess)

        // 自检书与收藏由 tearDown 按各自的随机身份清理。
    }

    @Test
    fun legacyBackupWithoutFavoritesPreservesExistingFavorites() {
        val favoritesFile = File(dataDir, "manga/_favorites.json")
        val progressFile = File(dataDir, "book_progress.json")
        val beforeFavorites = favoritesFile.takeIf { it.isFile }?.readBytes()
        val beforeProgress = progressFile.takeIf { it.isFile }?.readBytes()
        favoritesFile.parentFile?.mkdirs()
        val currentFavoriteKey = "mangadex:current-favorite-${java.util.UUID.randomUUID()}"
        backupFavoriteKey = currentFavoriteKey
        val currentFavoritesObject = if (favoritesFile.isFile) JSONObject(favoritesFile.readText(Charsets.UTF_8))
            else JSONObject()
        currentFavoritesObject.put(currentFavoriteKey, JSONObject().put("title", "设备收藏"))
        val currentFavorites = currentFavoritesObject.toString().toByteArray(Charsets.UTF_8)
        favoritesFile.writeBytes(currentFavorites)

        // 历史格式 v1 没有 manga/_favorites.json：缺字段代表“旧版本不支持”，不是清空。
        val legacyProgress = "{\"legacy-book\":{\"idx\":7,\"pct\":42}}"
            .toByteArray(Charsets.UTF_8)
        val legacyZip = artifact("selftest-legacy-backup")
        val legacyManifest = JSONObject().apply {
            put("format", Backup.FORMAT)
            put("created_at", "2025-01-02 03:04:05")
            put("entries", JSONArray().put(JSONObject().apply {
                put("path", "book_progress.json")
                put("size", legacyProgress.size)
                put("sha256", sha256(legacyProgress))
            }))
        }.toString().toByteArray(Charsets.UTF_8)
        writeZip(legacyZip, linkedMapOf(
            Backup.MANIFEST to legacyManifest,
            "book_progress.json" to legacyProgress,
        ))

        try {
            repeat(2) {
                val report = kotlinx.coroutines.runBlocking {
                    Backup.restore(ctx, Uri.fromFile(legacyZip))
                }
                assertEquals("旧备份只应恢复它包含的进度文件", 1, report.restored)
                assertEquals("收藏不在旧备份中时必须保留设备现有收藏",
                    currentFavorites.toList(), favoritesFile.readBytes().toList())
                assertEquals("旧版阅读进度应被恢复", legacyProgress.toList(),
                    progressFile.readBytes().toList())
            }
        } finally {
            if (beforeFavorites != null) favoritesFile.writeBytes(beforeFavorites)
            else favoritesFile.delete()
            if (beforeProgress != null) progressFile.writeBytes(beforeProgress)
            else progressFile.delete()
            legacyZip.delete()
        }
    }

    @Test
    fun restoreRejectsDuplicatePathsAndOversizedCompressedEntries() {
        val target = File(dataDir, "book_progress.json")
        val before = target.takeIf { it.isFile }?.readBytes()
        val duplicate = artifact("selftest-backup-duplicate")
        val small = "{}".toByteArray(Charsets.UTF_8)
        val duplicateManifest = JSONObject().apply {
            put("format", Backup.FORMAT)
            put("created_at", "2026-10-02 00:00:00")
            put("entries", JSONArray()
                .put(JSONObject().put("path", "book_progress.json").put("size", small.size).put("sha256", sha256(small)))
                .put(JSONObject().put("path", "book_progress.json").put("size", small.size).put("sha256", sha256(small))))
        }.toString().toByteArray(Charsets.UTF_8)
        writeZipEntries(duplicate, listOf(Backup.MANIFEST to duplicateManifest,
            "book_progress.json" to small))
        try {
            val duplicateError = runCatching {
                kotlinx.coroutines.runBlocking { Backup.restore(ctx, Uri.fromFile(duplicate)) }
            }.exceptionOrNull()
            assertTrue("重复清单路径必须拒绝", duplicateError?.message?.contains("重复") == true)

            val oversized = artifact("selftest-backup-oversized")
            val manifest = JSONObject().apply {
                put("format", Backup.FORMAT)
                put("created_at", "2026-10-02 00:00:00")
                put("entries", JSONArray().put(JSONObject().put("path", "book_progress.json")))
            }.toString().toByteArray(Charsets.UTF_8)
            // Highly compressible payload: ZIP 的压缩大小不能绕过解压后限额。
            writeZipEntries(oversized, listOf(Backup.MANIFEST to manifest,
                "book_progress.json" to ByteArray(16 * 1024 * 1024 + 1)))
            val sizeError = runCatching {
                kotlinx.coroutines.runBlocking { Backup.restore(ctx, Uri.fromFile(oversized)) }
            }.exceptionOrNull()
            assertTrue("超限压缩条目必须在恢复前拒绝", sizeError?.message?.contains("过大") == true)
            assertEquals("拒绝恶意备份不得改动现有进度", before?.toList(),
                target.takeIf { it.isFile }?.readBytes()?.toList())
        } finally {
            if (before != null) target.writeBytes(before) else target.delete()
            duplicate.delete()
        }
    }

    @Test
    fun crossProcessFixtureCleanupPreservesOtherUserRecords() {
        val runId = java.util.UUID.randomUUID().toString()
        val fixtureId = java.util.UUID.randomUUID().toString()
        val preservedLibraryId = "preserve-library-$runId"
        val preservedHistoryKey = "mangadex:preserve-history-$runId"
        val preservedFavoriteKey = "mangadex:preserve-favorite-$runId"
        val laterProcessFixture = SelfTestComic(comicId = fixtureId)
        val libraryFile = File(dataDir, "manga/_library.json")
        val historyFile = File(dataDir, "manga/_history.json")
        val favoritesFile = File(dataDir, "manga/_favorites.json")
        try {
            laterProcessFixture.create()

            val library = JSONArray(libraryFile.readText(Charsets.UTF_8))
            library.put(JSONObject().put("source", "mangadex").put("comic_id", preservedLibraryId)
                .put("title", "必须保留的其他书目"))
            libraryFile.writeText(library.toString(), Charsets.UTF_8)

            val history = if (historyFile.isFile) JSONObject(historyFile.readText(Charsets.UTF_8))
                else JSONObject()
            history.put(preservedHistoryKey, JSONObject().put("title", "必须保留的历史"))
            historyFile.writeText(history.toString(), Charsets.UTF_8)

            val favorites = if (favoritesFile.isFile) JSONObject(favoritesFile.readText(Charsets.UTF_8))
                else JSONObject()
            favorites.put(preservedFavoriteKey, JSONObject().put("title", "必须保留的收藏"))
            favoritesFile.writeText(favorites.toString(), Charsets.UTF_8)

            // 模拟另一个 instrumentation 进程：没有 create() 时保存的整文件快照。
            laterProcessFixture.cleanupFixtureOnly()

            val remainingLibrary = JSONArray(libraryFile.readText(Charsets.UTF_8))
            assertTrue((0 until remainingLibrary.length()).any {
                remainingLibrary.optJSONObject(it)?.optString("comic_id") == preservedLibraryId
            })
            assertFalse((0 until remainingLibrary.length()).any {
                remainingLibrary.optJSONObject(it)?.optString("comic_id") == fixtureId
            })
            val remainingHistory = JSONObject(historyFile.readText(Charsets.UTF_8))
            assertTrue(remainingHistory.has(preservedHistoryKey))
            assertFalse(remainingHistory.has("mangadex:$fixtureId"))
            val remainingFavorites = JSONObject(favoritesFile.readText(Charsets.UTF_8))
            assertTrue(remainingFavorites.has(preservedFavoriteKey))
            assertFalse(remainingFavorites.has("mangadex:$fixtureId"))
        } finally {
            // This test can run after a failed assertion too. Remove only identities
            // generated above; never restore a stale whole-file snapshot over newer data.
            runCatching { laterProcessFixture.cleanupFixtureOnly() }
            if (libraryFile.isFile) runCatching {
                val rows = JSONArray(libraryFile.readText(Charsets.UTF_8))
                val kept = JSONArray()
                for (i in 0 until rows.length()) {
                    val row = rows.optJSONObject(i) ?: continue
                    if (row.optString("comic_id") != preservedLibraryId) kept.put(row)
                }
                if (kept.length() == 0) libraryFile.delete()
                else libraryFile.writeText(kept.toString(), Charsets.UTF_8)
            }
            listOf(historyFile to preservedHistoryKey,
                favoritesFile to preservedFavoriteKey).forEach { (file, key) ->
                if (file.isFile) runCatching {
                    val data = JSONObject(file.readText(Charsets.UTF_8))
                    data.remove(key)
                    if (data.length() == 0) file.delete()
                    else file.writeText(data.toString(), Charsets.UTF_8)
                }
            }
        }
    }

    private fun readZip(f: File): LinkedHashMap<String, ByteArray> {
        val out = LinkedHashMap<String, ByteArray>()
        java.util.zip.ZipInputStream(f.inputStream().buffered()).use { zis ->
            var e = zis.nextEntry
            while (e != null) {
                out[e.name] = zis.readBytes()
                zis.closeEntry()
                e = zis.nextEntry
            }
        }
        return out
    }

    private fun writeZip(f: File, entries: Map<String, ByteArray>) {
        writeZipEntries(f, entries.entries.map { it.key to it.value })
    }

    private fun writeZipEntries(f: File, entries: List<Pair<String, ByteArray>>) {
        ZipOutputStream(f.outputStream().buffered()).use { zos ->
            entries.forEach { (name, bytes) ->
                zos.putNextEntry(ZipEntry(name))
                zos.write(bytes)
                zos.closeEntry()
            }
        }
    }

    private fun sha256(bytes: ByteArray): String =
        java.security.MessageDigest.getInstance("SHA-256").digest(bytes)
            .joinToString("") { "%02x".format(it) }
}
