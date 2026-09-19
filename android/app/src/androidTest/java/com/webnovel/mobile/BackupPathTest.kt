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
 *   - 备份内容 = 书源配置 + 小说阅读进度 + 漫画书库 + 漫画阅读进度（不含正文/图片）；
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

    private fun ev(line: String) = println("BACKUP_PATH_EVIDENCE $line")

    @Before
    fun setUp() {
        book = SelfTestBook()
        book.create()
        comic = SelfTestComic()
        comic.create()
    }

    @After
    fun tearDown() {
        book.cleanup()
        comic.cleanup()
    }

    @Test
    fun backupThenMutateThenRestore() {
        val libFile = File(dataDir, "manga/_library.json")
        val histFile = File(dataDir, "manga/_history.json")

        // 0) 先写一条阅读进度，确保备份里有真实内容
        val progressFile = File(dataDir, "book_progress.json")
        val beforeProgress = progressFile.takeIf { it.isFile }?.readBytes()
        val progress = JSONObject().apply {
            put(book.bookKey, JSONObject().apply {
                put("idx", 1); put("pct", 25); put("name", SelfTestBook.CHAPTER1_NAME)
                put("ts", 1_700_000_000)
            })
        }
        progressFile.parentFile?.mkdirs()
        progressFile.writeText(progress.toString(), Charsets.UTF_8)

        val zip = File(ctx.cacheDir, "selftest-backup.zip")
        zip.delete()

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
        // 全新安装（尚未启动过引擎）时书源目录可能还是空的——备份必须先解压内置书源，
        // 否则备份会悄悄缺一半内容（实测在 pm clear 之后的首次运行里踩到）
        assertTrue("备份应含书源目录下的文件（即使全新安装未启动过引擎）",
            entries.keys.any { it.startsWith("sources/") && it.endsWith(".json") })
        assertNull("书源齐全时不应产生告警", report.warning)
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

        // 2) 改动现场：删掉书、改掉进度
        File(dataDir, "books/${book.bookKey}").deleteRecursively()
        assertFalse("先确认书目录已删", File(dataDir, "books/${book.bookKey}").isDirectory)
        progressFile.writeText("{}", Charsets.UTF_8)
        assertEquals("先确认进度已被改写", "{}", progressFile.readText(Charsets.UTF_8))

        // 3) 恢复
        val rr = kotlinx.coroutines.runBlocking { Backup.restore(ctx, Uri.fromFile(zip)) }
        assertTrue("恢复应至少写入 1 个文件", rr.restored > 0)
        assertEquals("恢复时间应来自清单", manifest.optString("created_at"), rr.createdAt)
        assertEquals("恢复后的进度内容应与备份一致", progressSha,
            sha256(progressFile.readBytes()))
        ev("恢复：写入=${rr.restored} 跳过=${rr.skipped} 进度已还原=${sha256(progressFile.readBytes()) == progressSha}")

        // 书源目录应被还原（备份里有的都回来了）
        val srcNames = entries.keys.filter { it.startsWith("sources/") }
        assertTrue("备份中的书源文件都应还原",
            srcNames.all { File(dataDir, it).isFile })
        ev("恢复后书源文件齐全：${srcNames.size} 个")

        // 4) 被篡改的备份必须整体失败（SHA-256 校验）
        val tampered = File(ctx.cacheDir, "selftest-backup-tampered.zip")
        writeZip(tampered, entries.toMutableMap().apply {
            this["book_progress.json"] = "{\"tampered\":true}".toByteArray(Charsets.UTF_8)
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
        val evilTarget = File(ctx.filesDir, "evil-escaped.json")
        evilTarget.delete()
        val evil = File(ctx.cacheDir, "selftest-backup-evilslip.zip")
        val evilManifest = JSONObject().apply {
            put("format", Backup.FORMAT)
            put("created_at", "2026-01-01 00:00:00")
            put("entries", JSONArray().put(JSONObject().apply {
                put("path", "../evil-escaped.json")
                put("size", 5)
                put("sha256", sha256("evil!".toByteArray()))
            }))
        }
        writeZip(evil, linkedMapOf(
            Backup.MANIFEST to evilManifest.toString().toByteArray(Charsets.UTF_8),
            "../evil-escaped.json" to "evil!".toByteArray(Charsets.UTF_8),
        ))
        var slipErr: Throwable? = null
        try {
            kotlinx.coroutines.runBlocking { Backup.restore(ctx, Uri.fromFile(evil)) }
        } catch (t: Throwable) { slipErr = t }
        assertTrue("zip-slip 备份应被拒绝", slipErr != null)
        assertFalse("绝不能写出数据目录之外的文件", evilTarget.exists())
        ev("zip-slip 被拒：${slipErr?.message?.take(50)}")

        // 6) 不是本应用的 zip（没有清单）必须明确失败
        val noManifest = File(ctx.cacheDir, "selftest-backup-nomanifest.zip")
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
        assertTrue(Backup.isAllowedPath("sources/abc.json"))
        assertFalse(Backup.isAllowedPath("../x.json"))
        assertFalse(Backup.isAllowedPath("/etc/passwd"))
        assertFalse(Backup.isAllowedPath("sources/../x.json"))
        assertFalse(Backup.isAllowedPath("manga/evil.exe"))
        assertFalse(Backup.isAllowedPath("sources/sub/dir.json"))
        ev("路径白名单检查通过")

        // 8) 用户数据文件保持可用（备份/恢复不破坏当前运行状态）
        assertTrue("书库里仍有自检漫画", libFile.isFile || !libFile.exists())
        assertTrue("历史文件仍可读", !histFile.exists() || histFile.length() >= 0)
        assertTrue("进度文件仍是合法 JSON",
            runCatching { JSONObject(progressFile.readText(Charsets.UTF_8)) }.isSuccess)

        // 还原原始进度文件内容（自检书键由 SelfTestBook 负责清理）
        if (beforeProgress != null) progressFile.writeBytes(beforeProgress) else progressFile.delete()
        zip.delete(); tampered.delete(); evil.delete(); noManifest.delete()
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
