package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import android.util.AtomicFile
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.security.MessageDigest
import java.util.UUID

/**
 * **覆盖升级一致性**验收（方向基线 §7.2 P1-C"升级后任务和文件一致"、§8.E
 * "同签名覆盖升级保存书库和进度"）。
 *
 * 为什么拆成两段：覆盖安装（`adb install -r`）不能在仪器化进程里对自己做，
 * 必须由外部执行。所以本类分为两个**阶段**，由外部脚本在中间完成安装：
 *
 *   外部先在空白专用 AVD 安装并运行旧 APK，再安装同签名 instrumentation APK。
 *   阶段 1 `phase1_setupState`（旧版本运行期间造状态）：经旧版引擎下载并**暂停**
 *            真实漫画任务，同时经旧版生产 API 建立已下载漫画书目、收藏与阅读进度，
 *            并模拟用户编辑书源；逐步写入恢复日志和最终证据。
 *   外部以 `adb install -r <新版 APK>` 覆盖安装，不卸载应用。
 *   阶段 2 `phase2_verifyAfterUpgrade`：逐项核对升级前后一致：
 *            · 书库条目还在、下载的图片**哈希未变**且仍能解码；
 *            · `_info.json` 仍可解析、章节列表包含那一话；
 *            · **暂停的任务没有擅自复活**（仍为 paused/stopped，磁盘张数不变）；
 *            · 阅读进度、收藏身份与漫画阅读位置原样保留；
 *            · 用户手动改过的源文件**一个字节都没变**（种子合并不得覆盖用户改动）。
 *
 * 只跑阶段 1（或只跑阶段 2）都会给出明确失败信息，不会假装通过；
 * shell harness 失败时可调用 `cleanupAfterFailure`，仅移除本轮夹具并还原源文件。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
// 阶段顺序必须确定：整包运行时 phase1 必须先造状态、phase2 才能核对
@org.junit.FixMethodOrder(org.junit.runners.MethodSorters.NAME_ASCENDING)
class UpgradeConsistencyTest {

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext
    private var source = "mangadex"
    private val upgradeHarness get() = InstrumentationRegistry.getArguments()
        .getString("upgradeHarness") == "1"

    private fun skipUnlessUpgradeRequired(message: String, condition: Boolean) {
        if (upgradeHarness) assertTrue(message, condition) else assumeTrue(message, condition)
    }

    private fun ev(line: String) = println("UPGRADE_EVIDENCE $line")

    private fun evidenceFile() = File(ctx.filesDir, "upgrade-check.json")
    private fun journalFile() = File(ctx.filesDir, "upgrade-check-journal.json")
    private fun phase1CompleteFile() = File(ctx.filesDir, "upgrade-check-phase1-complete")
    private fun sourcesDir() = File(OfflineStore.runtimeDir(ctx), "sources")

    private fun checkpoint(key: String, value: String) {
        val file = journalFile()
        val journal = if (file.isFile) JSONObject(file.readText(Charsets.UTF_8))
            else JSONObject()
        journal.put(key, value)
        writeAtomic(file, journal.toString().toByteArray(Charsets.UTF_8))
    }

    private fun writeAtomic(file: File, bytes: ByteArray) {
        file.parentFile?.mkdirs()
        val atomic = AtomicFile(file)
        val stream = atomic.startWrite()
        try {
            stream.write(bytes)
            atomic.finishWrite(stream)
        } catch (t: Throwable) {
            atomic.failWrite(stream)
            throw t
        }
    }

    private fun safeIdentityPart(value: String): Boolean =
        value.isNotBlank() && "^[A-Za-z0-9._-]+$".toRegex().matches(value) &&
            !value.contains("..")

    private fun removeObjectIdentity(file: File, key: String) {
        if (!file.isFile) return
        val value = runCatching { JSONObject(file.readText(Charsets.UTF_8)) }.getOrNull() ?: return
        value.remove(key)
        if (value.length() == 0) file.delete()
        else writeAtomic(file, value.toString().toByteArray(Charsets.UTF_8))
    }

    private fun removePriorCleanupSentinel(file: File, key: String, expectedTitle: String) {
        if (!file.isFile) return
        val value = runCatching { JSONObject(file.readText(Charsets.UTF_8)) }.getOrNull() ?: return
        if (value.optJSONObject(key)?.optString("title") != expectedTitle) return
        value.remove(key)
        if (value.length() == 0) file.delete()
        else writeAtomic(file, value.toString().toByteArray(Charsets.UTF_8))
    }

    private fun removeLibraryIdentity(source: String, comicId: String) {
        val file = File(OfflineStore.runtimeDir(ctx), "manga/_library.json")
        if (!file.isFile) return
        val array = runCatching { JSONArray(file.readText(Charsets.UTF_8)) }.getOrNull() ?: return
        val kept = JSONArray()
        for (i in 0 until array.length()) {
            val item = array.optJSONObject(i) ?: continue
            if (item.optString("source") != source || item.optString("comic_id") != comicId) {
                kept.put(item)
            }
        }
        if (kept.length() == 0) file.delete()
        else writeAtomic(file, kept.toString().toByteArray(Charsets.UTF_8))
    }

    private fun objectHasIdentity(file: File, key: String): Boolean =
        file.isFile && runCatching {
            JSONObject(file.readText(Charsets.UTF_8)).has(key)
        }.getOrDefault(true)

    private fun libraryHasIdentity(source: String, comicId: String): Boolean {
        val file = File(OfflineStore.runtimeDir(ctx), "manga/_library.json")
        if (!file.isFile) return false
        val array = runCatching { JSONArray(file.readText(Charsets.UTF_8)) }.getOrNull()
            ?: return true
        return (0 until array.length()).any { i ->
            val item = array.optJSONObject(i)
            item?.optString("source") == source && item.optString("comic_id") == comicId
        }
    }

    private fun sha(f: File): String =
        MessageDigest.getInstance("SHA-256").digest(f.readBytes())
            .joinToString("") { "%02x".format(it) }

    private fun isImage(f: File): Boolean {
        if (!f.isFile || f.length() < 1000) return false
        val b = ByteArray(12)
        return try {
            f.inputStream().use { it.read(b) }
            (b[0] == 0xFF.toByte() && b[1] == 0xD8.toByte()) ||
                (b[0] == 0x89.toByte() && b[1] == 'P'.code.toByte()) ||
                (String(b, 0, 4) == "RIFF") || (String(b, 0, 3) == "GIF")
        } catch (t: Throwable) { false }
    }

    private fun decodable(f: File): Boolean {
        val o = android.graphics.BitmapFactory.Options().apply { inJustDecodeBounds = true }
        android.graphics.BitmapFactory.decodeFile(f.absolutePath, o)
        return o.outWidth > 0 && o.outHeight > 0
    }

    private fun chapterDir(comicId: String, chapterId: String) = File(
        OfflineStore.runtimeDir(ctx), "manga/downloads/$source/$comicId/$chapterId")

    // ── 阶段 1：造状态 ──────────────────────────────────────────────────

    @Test
    fun phase1_setupState(): Unit = runBlocking {
        // 阶段 2 不能拿上次执行留下的证据误判通过；新一轮必须由本轮阶段 1 重新造数。
        check(!evidenceFile().exists() || evidenceFile().delete()) {
            "无法清理上一轮升级证据：${evidenceFile()}"
        }
        journalFile().delete()
        phase1CompleteFile().delete()
        File(ctx.filesDir, "upgrade-check-orig.json").delete()
        val gateway = EngineGateway(ctx)
        val st = gateway.connect()
        assertTrue("引擎未就绪：$st", st is EngineState.Ready)
        val ep = (st as EngineState.Ready).endpoint

        // 1) 真实源：搜索 → 详情 → 建下载任务（真实链路，不用夹具）
        // 夹具解析：优先缓存、失败才搜索（源站限流不再让本用例成片失败）
        val fx = MangaFixture.pick(gateway, ep.port, source,
            requireReachable = upgradeHarness) { ev(it) }
        source = fx.source
        val realComicId = fx.comicId
        val comicId = "__upgrade_${UUID.randomUUID().toString().replace("-", "")}__"
        val chapterId = fx.chapterId
        assertTrue("升级夹具来源需可稳定路由", source == "mangadex")
        checkpoint("source", source)
        checkpoint("comic_id", comicId)
        checkpoint("chapter_id", chapterId)
        checkpoint("real_source_comic_id", realComicId)
        ev("选中真实源章节：$source/$realComicId / ${fx.chapterId}；本地身份隔离为 $comicId")

        val detailResp = gateway.httpText(ep.port, "/api/manga/$source/$realComicId")
        assertTrue("读取下载目录应成功：HTTP ${detailResp.code}", detailResp.ok)
        val detail = EngineData.mangaDetail(detailResp.body)
            ?: throw AssertionError("下载目录无法解析")
        // 多话任务使“首批图片写盘后暂停”有足够观察窗口。保留夹具选中的话为第一项，
        // 后续章节取同一真实作品目录；不再让单话快速完成与两秒轮询发生竞态。
        val chapterIds = (listOf(chapterId) + detail.readingChapters.map { it.id })
            .filter { it.isNotBlank() }
            .distinct()
            .take(12)
        assertTrue("升级夹具至少要有两话，才能验证中途暂停", chapterIds.size >= 2)
        val syntheticDir = File(OfflineStore.runtimeDir(ctx), "manga/downloads/$source/$comicId")
        check(!syntheticDir.exists()) { "升级测试随机目录意外已存在：$syntheticDir" }
        check(syntheticDir.mkdirs()) { "无法创建升级测试隔离目录：$syntheticDir" }
        val chapterRows = JSONArray()
        for (id in chapterIds) {
            val chapter = detail.readingChapters.firstOrNull { it.id == id } ?: continue
            chapterRows.put(JSONObject().put("id", chapter.id)
                .put("name", chapter.name).put("group", chapter.group))
        }
        File(syntheticDir, "_info.json").writeText(JSONObject()
            .put("title", detail.title).put("cover", detail.cover)
            .put("chapters", chapterRows).toString(), Charsets.UTF_8)
        val body = JSONObject().put("title", fx.title).put("cover", fx.cover)
            .put("chapters", JSONArray(chapterIds)).toString()
        val ok = gateway.httpPost(ep.port, "/api/manga/$source/$comicId/download", body)
        assertTrue("创建下载任务应 2xx：HTTP ${ok.code}", ok.code in 200..299)

        // 2) 等真有几张图落盘，然后**暂停**（模拟"用户停了一半，然后升级"）
        val dir = chapterDir(comicId, chapterId)
        var n = 0
        for (i in 0 until 240) {
            delay(250)
            n = dir.listFiles().orEmpty().count { isImage(it) }
            if (n >= 1) break
            if (taskStatus(gateway, ep.port, comicId) == "error") {
                skipUnlessUpgradeRequired("漫画源不可用，无法执行升级下载断言：$comicId", false)
            }
        }
        assertTrue("下载应至少落盘 1 张（实际 $n）", n >= 1)
        gateway.httpPost(ep.port, "/api/manga/download/pause?source=$source&cid=$comicId")
        var pausedStatus = ""
        for (i in 0 until 60) {
            delay(1000)
            pausedStatus = taskStatus(gateway, ep.port, comicId)
            if (pausedStatus in listOf("paused", "stopped")) break
        }
        assertTrue("暂停应生效（实际 $pausedStatus）", pausedStatus in listOf("paused", "stopped"))

        // 3) 写阅读进度（第 1 话第 3 页）——升级后必须原样保留
        val hp = gateway.httpPost(ep.port, "/api/manga/history", JSONObject()
            .put("source", source).put("comic_id", comicId)
            .put("idx", 0).put("pos", EngineData.mangaPos(fx.chapterId, 3))
            .put("title", fx.title).toString())
        assertTrue("写进度应成功：HTTP ${hp.code}", hp.ok)

        // 3b) 另造一部**已完成下载**的本地夹具：书库条目 + 进度 + 图片都在本机，
        //     用来验证"覆盖升级保存书库和进度"（真实任务的暂停状态单列一条验证——
        //     注意：任务未完成时**不会**写书库条目，这是设计如此，不是缺陷）
        // MangaDex 身份规范为 UUID；使用合法随机 ID，让收藏 API 覆盖真实身份规则。
        val fixture = SelfTestComic(comicId = java.util.UUID.randomUUID().toString())
        checkpoint("fixture_comic", fixture.comicIdValue)
        fixture.create()
        assertTrue("夹具书库条目应存在", fixture.libraryEntryCount() == 1)
        val fixtureFavorite = gateway.httpPost(ep.port, "/api/manga/favorites", JSONObject()
            .put("source", fixture.sourceKey).put("comic_id", fixture.comicIdValue)
            .put("title", SelfTestComic.TITLE).toString())
        assertTrue("旧版本收藏 API 应成功：HTTP ${fixtureFavorite.code}", fixtureFavorite.ok)
        assertTrue("旧版本收藏 API 必须确认写入：${fixtureFavorite.body}",
            fixtureFavorite.body.contains("\"ok\": true") ||
                fixtureFavorite.body.contains("\"ok\":true"))
        val fixtureHistory = gateway.httpPost(ep.port, "/api/manga/history", JSONObject()
            .put("source", fixture.sourceKey).put("comic_id", fixture.comicIdValue)
            .put("idx", 1).put("pos", EngineData.mangaPos(SelfTestComic.CH2_NAME, 1))
            .put("chapter_id", SelfTestComic.CH2_ID).put("chapter_label", SelfTestComic.CH2_NAME)
            .put("title", SelfTestComic.TITLE).toString())
        assertTrue("旧版本阅读历史 API 应成功：HTTP ${fixtureHistory.code}", fixtureHistory.ok)
        val fixtureLibrary = gateway.httpText(ep.port, "/api/manga/library")
        assertTrue("旧版本书库 API 应读取到漫画：HTTP ${fixtureLibrary.code}",
            fixtureLibrary.ok && fixtureLibrary.body.contains(fixture.comicIdValue))
        val fixtureHistoryRead = gateway.httpText(ep.port, "/api/manga/history")
        assertTrue("旧版本历史 API 应读取到漫画：HTTP ${fixtureHistoryRead.code}",
            fixtureHistoryRead.ok && fixtureHistoryRead.body.contains(fixture.comicIdValue))
        val fixtureRoot = File(OfflineStore.runtimeDir(ctx),
            "manga/downloads/${fixture.sourceKey}/${fixture.comicIdValue}")
        val fixtureImages = fixtureRoot.walkTopDown()
            .filter { it.isFile && isImage(it) }
            .sortedBy { it.relativeTo(fixtureRoot).invariantSeparatorsPath }
            .toList()
        assertEquals("升级前书库夹具应有预期的三张有效图片", 3, fixtureImages.size)
        val fixtureHashes = JSONObject().apply {
            fixtureImages.forEach { image ->
                put(image.relativeTo(fixtureRoot).invariantSeparatorsPath, sha(image))
            }
        }

        // 3c) 合成一本真实目录结构的小说，验证升级不会只保住漫画数据：
        // 书架扫描、已缓存正文和独立小说阅读进度都必须由新版原样读回。
        val novelKey = "upgrade_${UUID.randomUUID().toString().replace("-", "")}"
        checkpoint("novel_book_key", novelKey)
        val novel = SelfTestBook(key = novelKey)
        novel.create()
        val novelProgress = gateway.httpPost(ep.port, novel.progressUrl,
            JSONObject().put("idx", 1).put("pct", 37)
                .put("name", SelfTestBook.CHAPTER1_NAME).toString())
        assertTrue("旧版本小说进度 API 应成功：HTTP ${novelProgress.code} ${novelProgress.body}",
            novelProgress.ok && JSONObject(novelProgress.body).optBoolean("ok"))
        val novelCache = File(OfflineStore.runtimeDir(ctx),
            "books/$novelKey/${SelfTestBook.cacheKeyOf("https://selftest.invalid/book/chapter-1.html")}.cache")
        assertTrue("小说正文缓存夹具应存在", novelCache.isFile && novelCache.length() > 0)
        val novelCacheSha = sha(novelCache)
        val novelsBefore = gateway.httpText(ep.port, "/api/books")
        assertTrue("旧版本小说书架 API 应成功：HTTP ${novelsBefore.code}",
            novelsBefore.ok && novelsBefore.body.contains(novelKey))

        // 4) 模拟用户改动一个内置源文件（升级合并绝不能覆盖用户的东西）
        val userFile = sourcesDir().listFiles { f ->
            f.isFile && f.name.endsWith(".json")
        }?.sortedBy { it.name }?.firstOrNull()
            ?: throw AssertionError("设备上没有源文件可改")
        val userOrig = userFile.readBytes()
        writeAtomic(File(ctx.filesDir, "upgrade-check-orig.json"), userOrig)
        checkpoint("user_source_file", userFile.name)
        val jo = JSONObject(userFile.readText(Charsets.UTF_8))
        jo.put("bookSourceComment", "用户自己改过（升级一致性用例）")
        writeAtomic(userFile, jo.toString().toByteArray(Charsets.UTF_8))
        val userHash = sha(userFile)

        // 4b) 把种子合并标记的**判定逻辑版本**改旧：模拟"这台设备上的标记是旧版判定写的"
        //     （评审点出的真实处境）。这样升级后新版本会**真的重跑一次合并**，
        //     我们才能验证"合并跑过了，而且没碰用户改过的文件"，而不是"没人动过"。
        val marker = File(sourcesDir(), ".seed-applied")
        var mkBefore = 0L
        if (marker.exists()) {
            val mk = JSONObject(marker.readText(Charsets.UTF_8))
            mkBefore = mk.optLong("applied_at")
            mk.put("logic", 1)
            writeAtomic(marker, mk.toString().toByteArray(Charsets.UTF_8))
        }

        val files = dir.listFiles().orEmpty().filter { isImage(it) }.sortedBy { it.name }
        val evJson = JSONObject()
            .put("created_at", System.currentTimeMillis())
            .put("source", source)
            .put("comic_id", comicId)
            .put("chapter_id", chapterId)
            .put("chapter_label", fx.chapterId)
            .put("title", fx.title)
            .put("images", files.size)
            .put("paused_status", pausedStatus)
            .put("history_pos", EngineData.mangaPos(fx.chapterId, 3))
            .put("user_source_file", userFile.name)
            .put("user_source_hash", userHash)
            .put("fixture_comic", fixture.comicIdValue)
            .put("fixture_title", SelfTestComic.TITLE)
            .put("fixture_chapter1", SelfTestComic.CH1_ID)
            .put("fixture_images", 3)     // 第 1 话 2 张 + 第 2 话 1 张
            .put("fixture_hashes", fixtureHashes)
            .put("fixture_history_pos", EngineData.mangaPos(SelfTestComic.CH2_NAME, 1))
            .put("novel_book_key", novelKey)
            .put("novel_cache_sha256", novelCacheSha)
            .put("novel_progress_idx", 1)
            .put("novel_progress_pct", 37)
            .put("marker_applied_at_before", mkBefore)
            .put("hashes", JSONObject().apply {
                files.forEach { put(it.name, sha(it)) }
            })
        writeAtomic(evidenceFile(), evJson.toString().toByteArray(Charsets.UTF_8))
        writeAtomic(phase1CompleteFile(), "complete".toByteArray(Charsets.UTF_8))
        ev("阶段 1 完成：$comicId 已落盘 ${files.size} 张，任务=$pausedStatus，" +
            "进度=第 1 话 P3，用户改过 ${userFile.name}")
        gateway.stopEngine()
    }

    // ── 阶段 2：升级后逐项核对 ──────────────────────────────────────────

    @Test
    fun phase2_verifyAfterUpgrade(): Unit = runBlocking {
        skipUnlessUpgradeRequired("阶段 1 未生成升级证据（可能因目标源不可用而跳过）",
            evidenceFile().exists())
        val e = JSONObject(evidenceFile().readText(Charsets.UTF_8))
        source = e.getString("source")
        val comicId = e.getString("comic_id")
        val chapterId = e.getString("chapter_id")
        val before = e.getJSONObject("hashes")
        val nBefore = e.getInt("images")

        val gateway = EngineGateway(ctx)
        val st = gateway.connect()
        assertTrue("升级后引擎应能就绪：$st", st is EngineState.Ready)
        val ep = (st as EngineState.Ready).endpoint
        ev("升级后引擎就绪 port=${ep.port} instance=${ep.instanceId.take(8)}")

        // 1) 书库条目还在（已完成下载的夹具；未完成的任务不写书库条目，见阶段 1 注释）
        val lib = gateway.httpText(ep.port, "/api/manga/library")
        assertTrue("书库应能读到：HTTP ${lib.code}", lib.ok)
        val libraryRows = JSONObject(lib.body).optJSONArray("comics") ?: JSONArray()
        val fixtureLibraryEntry = (0 until libraryRows.length()).mapNotNull {
            libraryRows.optJSONObject(it)
        }.firstOrNull {
            it.optString("source") == "mangadex" &&
                it.optString("comic_id") == e.getString("fixture_comic")
        }
        assertTrue("升级后书库必须保留精确的源/作品身份（夹具 ${e.getString("fixture_title")}）",
            fixtureLibraryEntry != null)
        ev("书库条目仍在（${e.getString("fixture_title")}）")

        // 2) 图片：一张不多不少、**哈希未变**、仍能解码
        val dir = chapterDir(comicId, chapterId)
        val files = dir.listFiles().orEmpty().filter { isImage(it) }.sortedBy { it.name }
        assertTrue("升级后图片不得丢失（升级前 $nBefore 张，现在 ${files.size} 张）",
            files.size >= nBefore)
        for (name in before.keys()) {
            val f = File(dir, name)
            assertTrue("升级后图片必须还在：$name", f.isFile)
            assertEquals("升级后图片内容不得变化：$name", before.getString(name), sha(f))
            assertTrue("升级后图片必须仍能解码：$name", decodable(f))
        }
        ev("图片 ${files.size} 张：哈希一致、逐张可解码")

        // 3) _info.json 仍可解析、章节列表包含那一话
        val info = File(OfflineStore.runtimeDir(ctx), "manga/downloads/$source/$comicId/_info.json")
        assertTrue("_info.json 必须还在", info.isFile)
        val infoJson = runCatching { JSONObject(info.readText(Charsets.UTF_8)) }
            .getOrElse { throw AssertionError("_info.json 升级后不可解析：${it.message}") }
        val chapters = infoJson.optJSONArray("chapters") ?: JSONArray()
        var found = false
        for (i in 0 until chapters.length()) {
            if (chapters.optJSONObject(i)?.optString("id") == chapterId) found = true
        }
        assertTrue("_info.json 的章节列表必须仍包含 $chapterId", found)
        ev("_info.json 可解析，章节列表完整")

        // 4) **暂停的任务不得擅自复活**（升级不该等于"用户点了继续"）
        val taskResponse = gateway.httpText(ep.port,
            "/api/manga/download/status?source=$source&cid=$comicId")
        assertTrue("升级后任务状态 API 应成功：HTTP ${taskResponse.code}", taskResponse.ok)
        val taskState = JSONObject(taskResponse.body)
        val initialStatus = taskState.optString("status")
        assertTrue("引擎启动后任务必须仍保留为暂停/停止（实际 $initialStatus）",
            initialStatus in listOf("paused", "stopped"))
        assertEquals("引擎启动后必须保留用户暂停意图", "user_pause",
            taskState.optString("stop_kind"))
        delay(3000)                       // 给"复活路径"留出时间
        val stableTaskResponse = gateway.httpText(ep.port,
            "/api/manga/download/status?source=$source&cid=$comicId")
        assertTrue("升级后任务状态复核应成功：HTTP ${stableTaskResponse.code}",
            stableTaskResponse.ok)
        val stableTask = JSONObject(stableTaskResponse.body)
        val status = stableTask.optString("status")
        assertTrue("升级后被用户暂停的任务记录必须保留且不得复活（实际 $status）",
            status in listOf("paused", "stopped"))
        assertEquals("升级不得丢失用户暂停意图", "user_pause",
            stableTask.optString("stop_kind"))
        val nAfter = dir.listFiles().orEmpty().count { isImage(it) }
        assertEquals("升级后磁盘张数不应变化（暂停状态）", nBefore, nAfter)
        ev("任务仍为 $status，磁盘张数不变（$nAfter）")

        // 4b) 夹具（已完成下载）的图片在升级后仍可解码、`_library.json` 仍可解析
        val fixture = SelfTestComic(comicId = e.getString("fixture_comic"))
        val fixDir = File(OfflineStore.runtimeDir(ctx),
            "manga/downloads/${fixture.sourceKey}/${e.getString("fixture_comic")}")
        val fixtureHashes = e.getJSONObject("fixture_hashes")
        val fixRootPath = fixDir.canonicalPath + File.separator
        for (relativePath in fixtureHashes.keys()) {
            val image = File(fixDir, relativePath)
            assertTrue("夹具图片路径必须限制在本地作品目录内：$relativePath",
                image.canonicalPath.startsWith(fixRootPath))
            assertTrue("升级后夹具图片必须仍存在：$relativePath", image.isFile)
            assertEquals("升级后夹具图片字节不得变化：$relativePath",
                fixtureHashes.getString(relativePath), sha(image))
            assertTrue("升级后夹具图片必须仍能解码：$relativePath", decodable(image))
        }
        val actualFixtureImages = fixDir.walkTopDown().count { it.isFile && isImage(it) }
        assertEquals("夹具图片数应一致，且不得丢失/增加",
            e.getInt("fixture_images"), actualFixtureImages)
        assertEquals("升级前记录的每张图片都应有 SHA-256", e.getInt("fixture_images"),
            fixtureHashes.length())
        ev("夹具图片 $actualFixtureImages 张：相对路径、SHA-256 与图像解码均一致")

        // 4c) 覆盖升级必须保留旧版本生产 API 建立的收藏与漫画已读位置。
        val favorites = gateway.httpText(ep.port, "/api/manga/favorites")
        assertTrue("升级后收藏 API 应成功：HTTP ${favorites.code}", favorites.ok)
        val favoriteRows = JSONObject(favorites.body).optJSONArray("favorites") ?: JSONArray()
        val fixtureFavoriteAfterUpgrade = (0 until favoriteRows.length()).mapNotNull {
            favoriteRows.optJSONObject(it)
        }.firstOrNull {
            it.optString("identity_source") == "mangadex" &&
                it.optString("comic_id") == e.getString("fixture_comic")
        }
        assertTrue("升级后应保留精确的旧版收藏身份：${favorites.body}",
            fixtureFavoriteAfterUpgrade != null)
        val fixtureHistoryResponse = gateway.httpText(ep.port, "/api/manga/history")
        assertTrue("升级后历史 API 应成功：HTTP ${fixtureHistoryResponse.code}",
            fixtureHistoryResponse.ok)
        val histories = JSONObject(fixtureHistoryResponse.body).optJSONArray("history") ?: JSONArray()
        val fixtureHistoryAfterUpgrade = (0 until histories.length()).mapNotNull { histories.optJSONObject(it) }
            .firstOrNull { it.optString("comic_id") == e.getString("fixture_comic") }
        assertTrue("升级后应保留精确的漫画历史身份", fixtureHistoryAfterUpgrade != null)
        assertEquals("旧版漫画已读位置应保留", e.getString("fixture_history_pos"),
            fixtureHistoryAfterUpgrade?.optString("pos"))
        val upgradedReadIds = fixtureHistoryAfterUpgrade?.optJSONArray("read_chapter_ids")
            ?: JSONArray()
        assertTrue("升级不得丢失已读章节身份 ${SelfTestComic.CH2_ID}",
            (0 until upgradedReadIds.length()).any {
                upgradedReadIds.optString(it) == SelfTestComic.CH2_ID
            })
        ev("旧版生产 API 建立的收藏、漫画位置及已读章节身份升级后仍存在")

        // 5) 阅读进度原样保留
        val hr = gateway.httpText(ep.port, "/api/manga/history")
        val arr = JSONObject(hr.body).optJSONArray("history") ?: JSONArray()
        var pos = ""
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            if (o.optString("comic_id") == comicId) pos = o.optString("pos")
        }
        assertEquals("阅读进度必须原样保留", e.getString("history_pos"), pos)
        ev("阅读进度保留：$pos")

        // 5b) 小说书架、下载正文缓存与小说续读位置属于独立持久化数据，逐项核对。
        val novelKeyAfterUpgrade = e.getString("novel_book_key")
        val novelsAfter = gateway.httpText(ep.port, "/api/books")
        assertTrue("升级后小说书架 API 应成功：HTTP ${novelsAfter.code}", novelsAfter.ok)
        val novelRows = JSONObject(novelsAfter.body).optJSONArray("books") ?: JSONArray()
        val novelRow = (0 until novelRows.length()).mapNotNull { novelRows.optJSONObject(it) }
            .firstOrNull { it.optString("key") == novelKeyAfterUpgrade }
        assertTrue("升级后小说书架应保留精确条目", novelRow != null)
        assertEquals("小说下载完成章数应保留", 1, novelRow?.optInt("done", -1))
        val chapterAfterUpgrade = gateway.httpText(ep.port,
            "/api/books/$novelKeyAfterUpgrade/chapter/1")
        assertTrue("升级后小说已下载章节应可读取：HTTP ${chapterAfterUpgrade.code}",
            chapterAfterUpgrade.ok)
        val chapterJson = JSONObject(chapterAfterUpgrade.body)
        assertTrue("升级后章节必须仍为本地已下载", chapterJson.optBoolean("downloaded"))
        assertTrue("升级后正文应与缓存夹具内容一致",
            chapterJson.optString("content").contains(SelfTestBook.CHAPTER1_BODY_1) &&
                chapterJson.optString("content").contains(SelfTestBook.CHAPTER1_BODY_2))
        val novelCacheAfter = File(OfflineStore.runtimeDir(ctx),
            "books/$novelKeyAfterUpgrade/${SelfTestBook.cacheKeyOf("https://selftest.invalid/book/chapter-1.html")}.cache")
        assertTrue("升级后小说正文缓存必须存在", novelCacheAfter.isFile)
        assertEquals("升级后小说正文缓存字节不得变化",
            e.getString("novel_cache_sha256"), sha(novelCacheAfter))
        val novelProgressAfter = gateway.httpText(ep.port,
            "/api/books/$novelKeyAfterUpgrade/progress")
        assertTrue("升级后小说进度 API 应成功：HTTP ${novelProgressAfter.code}",
            novelProgressAfter.ok)
        val novelProgressJson = JSONObject(novelProgressAfter.body)
        assertEquals("小说续读章节应保留", e.getInt("novel_progress_idx"),
            novelProgressJson.optInt("idx", -1))
        assertEquals("小说章内进度应保留", e.getInt("novel_progress_pct"),
            novelProgressJson.optInt("pct", -1))
        assertEquals("小说续读章节名应保留", SelfTestBook.CHAPTER1_NAME,
            novelProgressJson.optString("name"))
        ev("小说书架、已下载正文缓存 SHA-256 与续读位置 ${novelProgressJson.optInt("pct")}% 均保留")

        // 6) 用户改过的源文件**一个字节都没变**（种子合并不得覆盖用户改动）
        val uf = File(sourcesDir(), e.getString("user_source_file"))
        assertTrue("用户改过的源文件必须还在：${uf.name}", uf.isFile)
        assertEquals("用户改过的源文件不得被升级合并覆盖",
            e.getString("user_source_hash"), sha(uf))
        ev("用户改过的源文件未被覆盖：${uf.name}")

        // 7) 新版本确实跑过种子合并（否则第 6 条只是"没人动过"）
        val marker = File(sourcesDir(), ".seed-applied")
        assertTrue("应有种子合并标记", marker.exists())
        val mk = JSONObject(marker.readText(Charsets.UTF_8))
        // 升级专属断言只在**外部真的做了覆盖安装**时强制：
        // 整包运行时（没有中间安装步骤）合并是幂等的、不会再跑，这不是缺陷。
        // 判据是运行参数 -e upgradeHarness 1（由覆盖升级脚本传入），
        // 不能靠"版本号变了"——覆盖升级前后版本号可能相同（降级再升回来）。
        if (upgradeHarness) {
            assertTrue("升级后应重新跑过种子合并（阶段 1 把标记 logic 改成 1；" +
                "applied_at=${mk.optLong("applied_at")} vs 阶段 1 时间=${e.getLong("created_at")}）",
                mk.optLong("applied_at") > e.getLong("created_at"))
            assertEquals("合并应记录当前判定逻辑版本", 2, mk.optInt("logic"))
            assertTrue("这一轮合并必须**看见并跳过**用户改过的文件（modified_skipped=" +
                "${mk.optInt("modified_skipped")}）", mk.optInt("modified_skipped") >= 1)
            ev("种子合并在升级后真的跑过：logic=${mk.optInt("logic")}，" +
                "出厂未改动 ${mk.optInt("untouched")}，用户改过而跳过 ${mk.optInt("modified_skipped")}")
        } else {
            ev("注意：本次**没有**经过覆盖安装（未传 -e upgradeHarness 1）→ " +
                "只核对数据一致性，升级专属断言（合并重跑）本次未验证；" +
                "marker logic=${mk.optInt("logic")} applied_at=${mk.optLong("applied_at")}")
        }

        // 清理：移除书库条目并删文件（不留测试下载）
        // 还原用例自己改过的源文件（阶段 1 备份了原文），别把设备留在被改状态
        val uf2 = File(sourcesDir(), e.getString("user_source_file"))
        val origBackup = File(ctx.filesDir, "upgrade-check-orig.json")
        if (origBackup.isFile) {
            writeAtomic(uf2, origBackup.readBytes())
            origBackup.delete()
            ev("已还原用例改过的源文件：${uf2.name}")
        }

        val rm = gateway.httpDelete(ep.port, "/api/manga/$source/$comicId/downloads")
        assertTrue("清理应 200：HTTP ${rm.code}", rm.ok)
        File(OfflineStore.runtimeDir(ctx), "manga/_cache/$source/$comicId")
            .deleteRecursively()
        val favoriteCleanup = gateway.httpDelete(ep.port,
            "/api/manga/favorites/${fixture.sourceKey}/${fixture.comicIdValue}")
        assertTrue("自检收藏清理应成功：HTTP ${favoriteCleanup.code}", favoriteCleanup.ok)
        fixture.cleanupFixtureOnly()
        SelfTestBook(key = novelKeyAfterUpgrade).cleanupFixtureOnly()
        assertTrue("夹具未清理干净", !fixture.exists())
        removeObjectIdentity(File(OfflineStore.runtimeDir(ctx), "manga/_history.json"),
            "$source:$comicId")
        removeObjectIdentity(File(OfflineStore.runtimeDir(ctx), "manga/_favorites.json"),
            "$source:$comicId")
        removeLibraryIdentity(source, comicId)
        evidenceFile().delete()
        journalFile().delete()
        phase1CompleteFile().delete()
        gateway.stopEngine()
        ev("阶段 2 通过并清理完成")
    }

    /** Safe to invoke explicitly from the shell harness after any failed phase. */
    @Test
    fun cleanupAfterFailure(): Unit = runBlocking {
        val journal = if (journalFile().isFile) runCatching {
            JSONObject(journalFile().readText(Charsets.UTF_8))
        }.getOrElse { throw AssertionError("升级恢复日志损坏；保留文件供人工恢复", it) }
            else JSONObject()
        val evidence = if (evidenceFile().isFile) runCatching {
            JSONObject(evidenceFile().readText(Charsets.UTF_8))
        }.getOrElse { throw AssertionError("升级阶段证据损坏；保留文件供人工恢复", it) }
            else JSONObject()
        fun recorded(key: String): String = evidence.optString(key).ifBlank {
            journal.optString(key)
        }

        val cleanupSource = recorded("source")
        val cleanupComic = recorded("comic_id")
        val cleanupFixture = recorded("fixture_comic")
        val cleanupNovel = recorded("novel_book_key")
        val sourceFilename = recorded("user_source_file")
        val originalSource = File(ctx.filesDir, "upgrade-check-orig.json")
        val hasWork = journal.length() > 0 || evidence.length() > 0 || originalSource.isFile
        if (!hasWork) {
            ev("失败清理无待处理记录")
            return@runBlocking
        }

        val gateway = EngineGateway(ctx)
        val state = runCatching { gateway.connect() }.getOrNull()
        val endpoint = (state as? EngineState.Ready)?.endpoint
        if (endpoint != null && safeIdentityPart(cleanupSource) &&
            safeIdentityPart(cleanupComic)) {
            runCatching {
                gateway.httpPost(endpoint.port,
                    "/api/manga/download/pause?source=$cleanupSource&cid=$cleanupComic")
                delay(500)
                gateway.httpDelete(endpoint.port,
                    "/api/manga/$cleanupSource/$cleanupComic/downloads")
            }
        }

        // Restore the edited source before any later JSON/path cleanup can fail.
        if (sourceFilename.isNotBlank() && !sourceFilename.contains("/") &&
            !sourceFilename.contains("\\") && !sourceFilename.contains("..") &&
            originalSource.isFile) {
            val sourceFile = File(sourcesDir(), sourceFilename)
            if (sourceFile.canonicalFile.parentFile == sourcesDir().canonicalFile) {
                writeAtomic(sourceFile, originalSource.readBytes())
            }
        }
        if (originalSource.isFile && sourceFilename.isNotBlank()) {
            val sourceFile = File(sourcesDir(), sourceFilename)
            assertTrue("失败清理未能还原用户源文件：$sourceFilename",
                sourceFile.isFile && sha(sourceFile) == sha(originalSource))
        }

        val runtimeManga = File(OfflineStore.runtimeDir(ctx), "manga")
        if (safeIdentityPart(cleanupSource) && safeIdentityPart(cleanupComic)) {
            File(runtimeManga, "downloads/$cleanupSource/$cleanupComic").deleteRecursively()
            File(runtimeManga, "_cache/$cleanupSource/$cleanupComic").deleteRecursively()
            removeLibraryIdentity(cleanupSource, cleanupComic)
            removeObjectIdentity(File(runtimeManga, "_history.json"),
                "$cleanupSource:$cleanupComic")
            removeObjectIdentity(File(runtimeManga, "_favorites.json"),
                "$cleanupSource:$cleanupComic")
        }
        if (safeIdentityPart(cleanupFixture)) {
            SelfTestComic(comicId = cleanupFixture).cleanupFixtureOnly()
        }
        if (safeIdentityPart(cleanupNovel)) {
            SelfTestBook(key = cleanupNovel).cleanupFixtureOnly()
        }

        if (safeIdentityPart(cleanupSource) && safeIdentityPart(cleanupComic)) {
            assertFalse("失败清理后下载目录仍残留",
                File(runtimeManga, "downloads/$cleanupSource/$cleanupComic").exists())
            assertFalse("失败清理后书库记录仍残留",
                libraryHasIdentity(cleanupSource, cleanupComic))
            assertFalse("失败清理后历史记录仍残留", objectHasIdentity(
                File(runtimeManga, "_history.json"), "$cleanupSource:$cleanupComic"))
            assertFalse("失败清理后收藏记录仍残留", objectHasIdentity(
                File(runtimeManga, "_favorites.json"), "$cleanupSource:$cleanupComic"))
        }
        if (safeIdentityPart(cleanupFixture)) {
            val fixtureSource = "mangadex"
            assertFalse("失败清理后本地漫画夹具仍残留",
                File(runtimeManga, "downloads/$fixtureSource/$cleanupFixture").exists())
            assertFalse("失败清理后夹具历史仍残留", objectHasIdentity(
                File(runtimeManga, "_history.json"), "$fixtureSource:$cleanupFixture"))
            assertFalse("失败清理后夹具收藏仍残留", objectHasIdentity(
                File(runtimeManga, "_favorites.json"), "$fixtureSource:$cleanupFixture"))
        }
        if (safeIdentityPart(cleanupNovel)) {
            val novelDir = File(OfflineStore.runtimeDir(ctx), "books/$cleanupNovel")
            assertFalse("失败清理后小说书目录仍残留", novelDir.exists())
            assertFalse("失败清理后小说阅读进度仍残留",
                objectHasIdentity(File(OfflineStore.runtimeDir(ctx), "book_progress.json"),
                    cleanupNovel))
        }
        originalSource.delete()
        evidenceFile().delete()
        journalFile().delete()
        phase1CompleteFile().delete()
        runCatching { gateway.stopEngine() }
        ev("失败后夹具下载/小说/书目/收藏/历史及用户书源已清理或还原")
    }

    @Test
    fun failureCleanupRestoresJournaledSourceAndPreservesOtherRecords(): Unit = runBlocking {
        val gateway = EngineGateway(ctx)
        val state = gateway.connect()
        assertTrue("清理故障注入测试需要本地引擎：$state", state is EngineState.Ready)

        val id = java.util.UUID.randomUUID().toString()
        val fixture = SelfTestComic(comicId = id)
        fixture.create()
        checkpoint("source", fixture.sourceKey)
        checkpoint("comic_id", id)
        checkpoint("chapter_id", SelfTestComic.CH1_ID)
        checkpoint("fixture_comic", id)

        val runtimeManga = File(OfflineStore.runtimeDir(ctx), "manga")
        val historyFile = File(runtimeManga, "_history.json")
        val favoritesFile = File(runtimeManga, "_favorites.json")
        // Remove the exact fixed sentinels left by older versions of this test.
        // New sentinels below are unique and always removed in finally.
        removePriorCleanupSentinel(historyFile, "mangadex:preserve-cleanup-history", "留存记录")
        removePriorCleanupSentinel(favoritesFile, "mangadex:preserve-cleanup-favorite", "留存收藏")
        val suffix = java.util.UUID.randomUUID().toString()
        val historySentinel = "mangadex:preserve-cleanup-history-$suffix"
        val favoriteSentinel = "mangadex:preserve-cleanup-favorite-$suffix"
        val history = if (historyFile.isFile) JSONObject(historyFile.readText(Charsets.UTF_8))
            else JSONObject()
        history.put("${fixture.sourceKey}:$id", JSONObject().put("title", SelfTestComic.TITLE))
        history.put(historySentinel, JSONObject().put("title", "留存记录"))
        writeAtomic(historyFile, history.toString().toByteArray(Charsets.UTF_8))
        val favorites = if (favoritesFile.isFile) JSONObject(favoritesFile.readText(Charsets.UTF_8))
            else JSONObject()
        favorites.put("${fixture.sourceKey}:$id", JSONObject().put("title", SelfTestComic.TITLE))
        favorites.put(favoriteSentinel, JSONObject().put("title", "留存收藏"))
        writeAtomic(favoritesFile, favorites.toString().toByteArray(Charsets.UTF_8))

        val sourceFile = sourcesDir().listFiles { f -> f.isFile && f.name.endsWith(".json") }
            ?.sortedBy { it.name }?.firstOrNull()
            ?: throw AssertionError("清理故障注入测试需要至少一个书源文件")
        val original = sourceFile.readBytes()
        val originalBackup = File(ctx.filesDir, "upgrade-check-orig.json")
        writeAtomic(originalBackup, original)
        checkpoint("user_source_file", sourceFile.name)
        val changed = JSONObject(sourceFile.readText(Charsets.UTF_8))
            .put("bookSourceComment", "cleanup-failure-injection")
        writeAtomic(sourceFile, changed.toString().toByteArray(Charsets.UTF_8))

        try {
            cleanupAfterFailure()

            assertTrue("用户源文件应逐字节恢复", sourceFile.isFile &&
                sourceFile.readBytes().contentEquals(original))
            assertFalse("自检漫画目录应清理", fixture.exists())
            assertTrue("其他历史记录应保留", JSONObject(historyFile.readText(Charsets.UTF_8))
                .has(historySentinel))
            assertTrue("其他收藏应保留", JSONObject(favoritesFile.readText(Charsets.UTF_8))
                .has(favoriteSentinel))
            assertFalse("自检历史应清理", objectHasIdentity(historyFile,
                "${fixture.sourceKey}:$id"))
            assertFalse("自检收藏应清理", objectHasIdentity(favoritesFile,
                "${fixture.sourceKey}:$id"))
            assertFalse("恢复日志应清理", journalFile().exists())
        } finally {
            removeObjectIdentity(historyFile, historySentinel)
            removeObjectIdentity(favoritesFile, favoriteSentinel)
        }
    }

    private suspend fun taskStatus(gw: EngineGateway, port: Int, comicId: String): String {
        val r = gw.httpText(port, "/api/manga/download/status?source=$source&cid=$comicId")
        return runCatching { JSONObject(r.body).optString("status") }.getOrDefault("")
    }
}
