package com.webnovel.mobile

import android.graphics.BitmapFactory
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.io.File

/**
 * CopyManga real-source integration check for a mixed volume/episode series.
 * Reads real source detail/image URLs, exercises a five-page fault boundary,
 * and downloads one full real volume on the dedicated API 35 AVD only.
 */
@RunWith(AndroidJUnit4::class)
class CopyMangaVolumeSourceTest {
    private lateinit var gateway: EngineGateway
    private val comicId = "xguangshideqiji"

    @Before
    fun setUp() = runBlocking {
        gateway = EngineGateway(InstrumentationRegistry.getInstrumentation().targetContext)
        val state = gateway.connect()
        assertTrue("本机引擎未就绪：$state", state is EngineState.Ready)
    }

    @After
    fun tearDown() {
        runCatching { runBlocking { gateway.stopEngine() } }
    }

    private fun enc(value: String): String = URLEncoder.encode(value, "UTF-8")

    @Test
    fun xRayRoom_mixedVolumesAndEpisodes_areSearchableAndReadable() = runBlocking {
        val endpoint = gateway.currentEndpoint()!!
        val search = gateway.httpText(endpoint.port,
            "/api/manga/search?source=copymanga&q=${enc("X光室的奇迹")}&page=1")
        assertTrue("拷贝漫画搜索失败 HTTP ${search.code}: ${search.body.take(200)}", search.ok)
        val hit = EngineData.mangaSearch(search.body).first.firstOrNull {
            it.comicId == "xguangshideqiji" && it.source == "copymanga"
        }
        assertNotNull("搜索结果中没有找到《X光室的奇迹》；设备端响应=" +
            search.body.take(600), hit)

        val detailResponse = gateway.httpText(endpoint.port,
            "/api/manga/copymanga/xguangshideqiji")
        assertTrue("作品详情失败 HTTP ${detailResponse.code}: " +
            detailResponse.body.take(200), detailResponse.ok)
        val detail = EngineData.mangaDetail(detailResponse.body)
        assertNotNull("详情不能解析", detail)
        detail!!
        assertEquals("X光室的奇跡", detail.title)
        assertEquals("预期 6 卷目录", 6, detail.volumes.size)
        assertTrue("详情应同时包含后续单话", detail.chapters.isNotEmpty())
        assertEquals("卷必须排在阅读目录前段", detail.volumes,
            detail.readingChapters.take(detail.volumes.size))
        assertEquals("卷之后必须进入单话目录", detail.chapters.first(),
            detail.readingChapters[detail.volumes.size])

        val volume = detail.volumes.first()
        val urlResponse = gateway.httpText(endpoint.port,
            "/api/manga/copymanga/xguangshideqiji/" +
                "chapter/${enc(volume.id)}/urls")
        assertTrue("第一卷图片目录失败 HTTP ${urlResponse.code}: " +
            urlResponse.body.take(200), urlResponse.ok)
        val imageList = JSONObject(urlResponse.body).optJSONArray("images")
        assertNotNull("卷图片目录缺少 images", imageList)
        assertTrue("第一卷图片目录为空", imageList!!.length() > 1)
        val first = imageList.getJSONObject(0)
        assertFalse("卷图片意外走入本地下载目录", first.optBoolean("local"))
        assertFalse("CopyManga 图片应为直接 CDN URL", first.optBoolean("lazy"))
        val imageUrl = first.optString("url")
        assertTrue("首张图片 URL 无效", imageUrl.startsWith("https://"))

        val connection = URL(imageUrl).openConnection() as HttpURLConnection
        val imageBytes = try {
            connection.connectTimeout = 15_000
            connection.readTimeout = 45_000
            connection.setRequestProperty("User-Agent",
                "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/124.0 Mobile Safari/537.36")
            connection.setRequestProperty("Referer", "https://www.copy4000.com/")
            assertEquals("卷图片 CDN 请求失败", 200, connection.responseCode)
            connection.inputStream.use { it.readBytes() }
        } finally {
            connection.disconnect()
        }
        assertTrue("图片响应体过小：${imageBytes.size}", imageBytes.size > 1_000)
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(imageBytes, 0, imageBytes.size, bounds)
        assertTrue("首张卷图片不能解码 (${bounds.outWidth}x${bounds.outHeight})",
            bounds.outWidth > 0 && bounds.outHeight > 0)
        println("COPYMANGA_VOLUME_EVIDENCE " +
            "title=${detail.title} volumes=${detail.volumes.size} " +
            "episodes=${detail.chapters.size} first=${volume.name} " +
            "images=${imageList.length()} fetched=1 bytes=${imageBytes.size} " +
            "decoded=${bounds.outWidth}x${bounds.outHeight}")
    }

    /** Real CopyManga pages through the production download worker, capped at five. */
    @Test
    fun xRayRoom_boundedVolumeSample_downloadsAndRemainsIncomplete(): Unit = runBlocking {
        val ep = gateway.currentEndpoint()!!
        val detailResponse = gateway.httpText(ep.port, "/api/manga/copymanga/$comicId")
        assertTrue("详情失败 HTTP ${detailResponse.code}", detailResponse.ok)
        val detail = EngineData.mangaDetail(detailResponse.body)
            ?: throw AssertionError("详情无法解析")
        assertEquals("预期 6 卷目录", 6, detail.volumes.size)

        val testComicId = "__codex_cm_volume_${java.util.UUID.randomUUID().toString().replace("-", "")}__"
        val root = File(OfflineStore.runtimeDir(
            InstrumentationRegistry.getInstrumentation().targetContext),
            "manga/downloads/copymanga/$testComicId")
        assertTrue("随机测试漫画目录意外已存在，拒绝覆盖", !root.exists())

        val candidates = ArrayList<Pair<MangaChapter, Int>>()
        for (volume in detail.volumes) {
            val r = gateway.httpText(ep.port,
                "/api/manga/copymanga/$comicId/chapter/${enc(volume.id)}/urls")
            assertTrue("卷 ${volume.name} 图片目录失败 HTTP ${r.code}", r.ok)
            val count = JSONObject(r.body).optJSONArray("images")?.length() ?: 0
            assertTrue("卷 ${volume.name} 为空", count > 0)
            candidates += volume to count
        }
        val (volume, expected) = candidates.minBy { it.second }
        val payload = JSONObject().put("comic_id", comicId)
            .put("chapter_id", volume.id).put("test_comic_id", testComicId)
            .put("volume_name", volume.name)
            .put("volume_pages", expected)
            .put("runtime_dir", OfflineStore.runtimeDir(
                InstrumentationRegistry.getInstrumentation().targetContext).absolutePath)
        val py = com.chaquo.python.Python.getInstance()
        val builtins = py.getModule("builtins")
        val scope = builtins.callAttr("dict")
        scope.callAttr("__setitem__", "__payload_json", payload.toString())
        val script = """
import json, os, time
from engine.manga.manager import get_adapter
import engine.manga.download_manager as dm
from engine.manga.base import MangaAdapter
from PIL import Image
payload = json.loads(__payload_json)
comic_id, chapter_id = payload['comic_id'], payload['chapter_id']
test_comic_id = payload['test_comic_id']
volume_pages = int(payload['volume_pages'])
ad = get_adapter('copymanga')
urls = ad.images(comic_id, chapter_id)
assert urls and len(urls) >= 5, 'real CopyManga image list unavailable'
sample = urls[:5]
class BoundedRealSourceAdapter(MangaAdapter):
    key = 'copymanga'
    name = '拷贝漫画（受限设备验收）'
    concurrent = 1
    def comic_info(self, requested_id):
        assert requested_id == test_comic_id
        return ad.comic_info(comic_id)
    def images(self, requested_id, requested_chapter):
        assert requested_id == test_comic_id and requested_chapter == chapter_id
        return sample
    def image_headers(self, image_url):
        return ad.image_headers(image_url)
    def chapters(self, requested_id):
        assert requested_id == test_comic_id
        from engine.manga.base import Chapter
        return [Chapter(id=chapter_id, name=payload['volume_name'], group='测试卷')]
runtime = payload['runtime_dir']
download_root = os.path.join(runtime, 'manga', 'downloads')
state_root = os.path.join(runtime, 'manga', '_state')
comic_root = os.path.join(download_root, 'copymanga', test_comic_id)
os.makedirs(os.path.dirname(comic_root), exist_ok=True)
assert not os.path.exists(comic_root), 'random download identity already exists'
original_get_adapter = dm._ensure_adapters
dm._ensure_adapters = lambda: None
manager = dm.DownloadManager(os.path.join(state_root, 'test-copy-volume-tasks.json'))
manager.configure(cache_root=os.path.join(runtime, 'manga', '_cache'),
                  state_dir=state_root,
                  library_file=os.path.join(runtime, 'manga', '_library.json'),
                  downloads_root=download_root)
manager._adapter_pool = {'copymanga': BoundedRealSourceAdapter()}
library_path = os.path.join(runtime, 'manga', '_library.json')
library_backup = library_path + '.bak'
library_before = open(library_path, 'rb').read() if os.path.exists(library_path) else None
backup_before = open(library_backup, 'rb').read() if os.path.exists(library_backup) else None
started = time.monotonic()
try:
    manager.start('copymanga', test_comic_id, 'CopyManga volume worker sample',
                  chapters=[{'id': chapter_id, 'name': payload['volume_name'],
                             'group': '测试卷'}], cover='')
    deadline = time.monotonic() + 120
    status = manager.status('copymanga:' + test_comic_id)
    while status.get('status') not in ('done', 'error', 'stopped', 'paused') and time.monotonic() < deadline:
        time.sleep(0.25)
        status = manager.status('copymanga:' + test_comic_id)
    assert status.get('status') == 'done', 'DownloadManager status: %r' % status
    assert status.get('images_done') == 5 and status.get('images_total') == 5, \
        'DownloadManager image progress mismatch: %r' % status
    chapter_dir = os.path.join(comic_root, chapter_id)
    images = sorted(os.path.join(chapter_dir, n) for n in os.listdir(chapter_dir)
                    if os.path.isfile(os.path.join(chapter_dir, n)) and n[:4].isdigit())
    assert len(images) == 5, 'downloaded file count mismatch: %d' % len(images)
    manifest = json.load(open(os.path.join(comic_root, '_info.json'), encoding='utf-8'))
    manifest_row = next(x for x in manifest['chapters'] if x.get('id') == chapter_id)
    assert manifest_row.get('download_page_count') == 5, 'worker page-count manifest mismatch'
    # Model the authoritative real source page count: this temporary task only
    # fetched five pages, so the production scanner must keep it incomplete.
    manifest_row['download_page_count'] = volume_pages
    with open(os.path.join(comic_root, '_info.json'), 'w', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False)
    import server.state as server_state
    import server.manga_api as manga_api
    assert chapter_id not in server_state._scan_downloaded_chapters('copymanga', test_comic_id), \
        'partial sample must not be reported as a fully downloaded chapter'
    local_dirs = manga_api._manga_read_local_dirs(
        'copymanga', test_comic_id, chapter_id, downloaded_only=True)
    assert len(local_dirs) == 1 and os.path.realpath(local_dirs[0]) == os.path.realpath(chapter_dir), \
        'local-only reader must resolve solely to the actual partial download directory'
    sizes = []
    for path in images:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            sizes.append('%dx%d' % im.size)
    __result = 'CM_DM_VOLUME_OK status=%s pages=%d manifest=%d decoded=%d ms=%d sizes=%s' % (
        status.get('status'), len(sample), manifest_row['download_page_count'],
        len(sizes), int((time.monotonic()-started)*1000), ','.join(sizes))
finally:
    dm._ensure_adapters = original_get_adapter
    key = 'copymanga:' + test_comic_id
    task = manager.status(key)
    if task.get('status') in ('running', 'queued'):
        manager.pause(key)
    worker = manager._threads.get(key)
    if worker and worker.is_alive():
        worker.join(30)
    assert not (worker and worker.is_alive()), 'test DownloadManager worker failed to stop'
    import shutil
    if os.path.isdir(comic_root):
        shutil.rmtree(comic_root)
    if library_before is None:
        try: os.remove(library_path)
        except OSError: pass
    else:
        with open(library_path, 'wb') as stream: stream.write(library_before)
    if backup_before is None:
        try: os.remove(library_backup)
        except OSError: pass
    else:
        with open(library_backup, 'wb') as stream: stream.write(backup_before)
    cache_comic_root = os.path.join(runtime, 'manga', '_cache', 'copymanga', test_comic_id)
    if os.path.isdir(cache_comic_root):
        shutil.rmtree(cache_comic_root)
    for path in (manager._state_file, manager._state_file + '.bak',
                 manager._state_file + '.reserve'):
        try: os.remove(path)
        except OSError: pass
__result
""".trimIndent()
        val result = builtins.callAttr("exec", script, scope)
        val evidence = scope.callAttr("get", "__result").toString()
        assertTrue("生产 DownloadManager 整卷任务（限 5 页）失败：$evidence",
            evidence.contains("CM_DM_VOLUME_OK"))
        assertFalse("随机测试漫画下载目录未清理", root.exists())
        assertFalse("随机测试漫画图片缓存未清理", File(
            OfflineStore.runtimeDir(InstrumentationRegistry.getInstrumentation().targetContext),
            "manga/_cache/copymanga/$testComicId").exists())
        val taskSnapshot = File(OfflineStore.runtimeDir(
            InstrumentationRegistry.getInstrumentation().targetContext),
            "manga/_state/test-copy-volume-tasks.json")
        assertFalse("随机测试任务快照残留", taskSnapshot.exists())
        assertFalse("随机测试任务备份残留", File(taskSnapshot.path + ".bak").exists())
        assertFalse("随机测试任务应急预留残留", File(taskSnapshot.path + ".reserve").exists())
        println("COPYMANGA_DOWNLOAD_MANAGER_SAMPLE volume=${volume.name} " +
            "fullPages=$expected sampled=5 ${evidence.take(300)}")
        Unit
    }

    /**
     * Full real-source acceptance on the disposable Upgrade_API35 AVD. The test
     * refuses to run if this exact volume already exists, snapshots user-facing
     * indexes, and removes only the newly created volume in finally.
     */
    @Test
    fun xRayRoom_realVolume_downloadsCompletelyAndIsLocalReadable(): Unit = runBlocking {
        val serial = android.os.Build.FINGERPRINT
        assumeTrue("仅允许专用 API 35 AVD", serial.contains("/emu64a:15/") &&
            android.os.Build.MODEL == "sdk_gphone64_arm64")
        val ep = gateway.currentEndpoint()!!
        val detailResponse = gateway.httpText(ep.port, "/api/manga/copymanga/$comicId")
        assertTrue("详情失败 HTTP ${detailResponse.code}", detailResponse.ok)
        val detail = EngineData.mangaDetail(detailResponse.body)
            ?: throw AssertionError("详情无法解析")
        val orderedVolume = detail.volumes.first()
        val urlResponse = gateway.httpText(ep.port,
            "/api/manga/copymanga/$comicId/chapter/${enc(orderedVolume.id)}/urls")
        assertTrue("卷图片目录失败 HTTP ${urlResponse.code}", urlResponse.ok)
        val images = JSONObject(urlResponse.body).optJSONArray("images")
            ?: throw AssertionError("卷图片清单缺失")
        val expectedPages = images.length()
        assertTrue("卷图片数异常：$expectedPages", expectedPages in 100..300)

        val runtime = OfflineStore.runtimeDir(
            InstrumentationRegistry.getInstrumentation().targetContext)
        val comicRoot = File(runtime, "manga/downloads/copymanga/$comicId")
        val chapterDir = File(comicRoot, orderedVolume.id)
        val additionalVolume = detail.volumes.drop(1).firstOrNull()
            ?: throw AssertionError("没有可用于增量下载验收的后续卷")
        val additionalChapterDir = File(comicRoot, additionalVolume.id)
        assumeTrue("真实卷目录已有内容，为保护用户数据拒绝覆盖: $chapterDir",
            !chapterDir.exists())
        assumeTrue("后续卷目录已有内容，为保护用户数据拒绝覆盖: $additionalChapterDir",
            !additionalChapterDir.exists())
        val comicRootExisted = comicRoot.isDirectory
        val beforeRootChildren = comicRoot.listFiles().orEmpty().toSet()
        val beforeRootFiles = beforeRootChildren
            .filter { it.isFile }.associateWith { it.readBytes() }
        val comicCacheRoot = File(runtime, "manga/_cache/copymanga/$comicId")
        val comicCacheExisted = comicCacheRoot.isDirectory
        val beforeCacheFiles = comicCacheRoot.walkTopDown()
            .filter { it.isFile }.associate { it to it.readBytes() }
        val state = File(runtime, "manga/_state")
        val snapshots = listOf(
            File(runtime, "manga/_library.json"), File(runtime, "manga/_library.json.bak"),
            File(runtime, "manga/_favorites.json"), File(runtime, "manga/_favorites.json.bak"),
            File(runtime, "manga/_history.json"), File(runtime, "manga/_history.json.bak"),
            File(runtime, "manga/_tasks.json"), File(runtime, "manga/_tasks.json.bak"),
            File(runtime, "manga/_tasks.json.reserve"),
        ).associateWith { file -> file.takeIf { it.isFile }?.readBytes() }
        var downloadStarted = false
        try {
            state.mkdirs()
            val request = JSONObject().put("title", detail.title)
                .put("cover", detail.cover)
                .put("chapters", JSONArray().put(orderedVolume.id))
            val start = gateway.httpPost(ep.port,
                "/api/manga/copymanga/$comicId/download", request.toString())
            assertTrue("创建整卷任务失败 HTTP ${start.code}: ${start.body.take(240)}",
                start.ok)
            downloadStarted = true
            val startedAt = System.currentTimeMillis()
            var task = JSONObject()
            val deadline = startedAt + 30 * 60_000L
            while (System.currentTimeMillis() < deadline) {
                val response = gateway.httpText(ep.port,
                    "/api/manga/download/status?source=copymanga&cid=$comicId")
                assertTrue("下载状态读取失败 HTTP ${response.code}", response.ok)
                task = JSONObject(response.body)
                val status = task.optString("status")
                if (status == "done" || status == "error" || status == "stopped") break
                kotlinx.coroutines.delay(1_000)
            }
            assertEquals("真实整卷任务未完成：${task}", "done", task.optString("status"))
            assertEquals("整卷页数与源目录不符", expectedPages,
                task.optInt("images_total", -1))
            assertEquals("存在未下载页面", expectedPages,
                task.optInt("images_done", -1))
            assertEquals("任务报告章节失败", 0, task.optInt("failed_chapters", 0))
            val favoriteBeforeRead = gateway.httpText(ep.port, "/api/manga/favorites")
            assertTrue("自动收藏读取失败 HTTP ${favoriteBeforeRead.code}", favoriteBeforeRead.ok)
            assertTrue("下载漫画没有自动加入收藏", EngineData.mangaFavorites(
                favoriteBeforeRead.body).any { it.source == "copymanga" && it.comicId == comicId })

            val updateResponse = gateway.httpPost(ep.port,
                "/api/manga/copymanga/$comicId/check-update", "{}")
            assertTrue("检查增量目录失败 HTTP ${updateResponse.code}", updateResponse.ok)
            val update = JSONObject(updateResponse.body)
            assertTrue("正式作品增量检查失败: ${update.optString("error")}",
                update.optBoolean("ok"))
            val missing = update.optJSONArray("missing") ?: JSONArray()
            val missingIds = (0 until missing.length()).mapNotNull {
                missing.optJSONObject(it)?.optString("id")
            }.toSet()
            assertFalse("已完整下载的卷仍被报告缺失", orderedVolume.id in missingIds)
            assertTrue("未下载的下一卷未出现在增量目录",
                detail.volumes.drop(1).any { it.id in missingIds })
            assertEquals("增量数量与源目录不符", update.optInt("new_total") - 1,
                update.optInt("missing_count"))

            val readSaved = gateway.httpPost(ep.port, "/api/manga/history", JSONObject()
                .put("source", "copymanga").put("comic_id", comicId)
                .put("idx", 0).put("pos", "${orderedVolume.name} P1")
                .put("chapter_id", orderedVolume.id).put("chapter_label", orderedVolume.name)
                .put("title", detail.title).toString())
            assertTrue("卷阅读历史保存失败 HTTP ${readSaved.code}", readSaved.ok)
            val favoriteAfterRead = gateway.httpText(ep.port, "/api/manga/favorites")
            assertTrue("收藏未读数读取失败 HTTP ${favoriteAfterRead.code}", favoriteAfterRead.ok)
            val favorite = EngineData.mangaFavorites(favoriteAfterRead.body)
                .single { it.source == "copymanga" && it.comicId == comicId }
            assertEquals("收藏未读数未按真实已读卷身份扣减",
                (detail.volumes.size + detail.chapters.size - 1).coerceAtLeast(0),
                favorite.unreadCount)

            val additionalUrlsResponse = gateway.httpText(ep.port,
                "/api/manga/copymanga/$comicId/chapter/${enc(additionalVolume.id)}/urls")
            assertTrue("下一卷图片清单失败 HTTP ${additionalUrlsResponse.code}",
                additionalUrlsResponse.ok)
            val additionalImages = JSONObject(additionalUrlsResponse.body)
                .optJSONArray("images") ?: throw AssertionError("下一卷图片清单缺失")
            val additionalExpected = additionalImages.length()
            assertTrue("下一卷图片数异常：$additionalExpected", additionalExpected in 100..300)
            val newUnit = JSONObject().put("id", additionalVolume.id)
                .put("name", additionalVolume.name).put("group", additionalVolume.group)
            val incrementalStart = gateway.httpPost(ep.port,
                "/api/manga/copymanga/$comicId/download-new", JSONObject()
                    .put("title", detail.title).put("cover", detail.cover)
                    .put("new_chapters", JSONArray().put(newUnit)).toString())
            assertTrue("真实增量下载 API 拒绝新卷 HTTP ${incrementalStart.code}: " +
                incrementalStart.body.take(240), incrementalStart.ok)
            val incrementalDeadline = System.currentTimeMillis() + 30 * 60_000L
            var incrementalTask = JSONObject()
            while (System.currentTimeMillis() < incrementalDeadline) {
                val response = gateway.httpText(ep.port,
                    "/api/manga/download/status?source=copymanga&cid=$comicId")
                assertTrue("增量任务状态读取失败 HTTP ${response.code}", response.ok)
                incrementalTask = JSONObject(response.body)
                if (incrementalTask.optString("status") in setOf("done", "error", "stopped")) {
                    break
                }
                kotlinx.coroutines.delay(1_000)
            }
            assertEquals("新增卷增量任务未完成：$incrementalTask", "done",
                incrementalTask.optString("status"))
            assertEquals("增量卷页数不完整", additionalExpected,
                incrementalTask.optInt("images_total", -1))
            assertEquals("增量卷未全部落盘", additionalExpected,
                incrementalTask.optInt("images_done", -1))
            val additionalFiles = additionalChapterDir.listFiles().orEmpty().filter {
                it.isFile && it.name.matches(
                    Regex("\\d+\\.(jpg|jpeg|png|webp)", RegexOption.IGNORE_CASE))
            }.sortedBy { it.name.substringBefore('.').toIntOrNull() ?: Int.MAX_VALUE }
            assertEquals("增量卷磁盘页数不符", additionalExpected, additionalFiles.size)
            additionalFiles.forEachIndexed { index, file ->
                val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                BitmapFactory.decodeFile(file.absolutePath, bounds)
                assertTrue("增量卷第 ${index + 1} 页解码失败",
                    file.length() > 1000L && bounds.outWidth > 0 && bounds.outHeight > 0)
            }
            val manifestChapters = JSONObject(File(comicRoot, "_info.json")
                .readText(Charsets.UTF_8)).optJSONArray("chapters")
                ?: throw AssertionError("增量下载后作品清单缺少 chapters")
            val pageCounts = (0 until manifestChapters.length()).mapNotNull { index ->
                manifestChapters.optJSONObject(index)?.let { row ->
                    row.optString("id") to row.optInt("download_page_count", -1)
                }
            }.toMap()
            assertEquals("增量刷新丢失旧卷的权威页数", expectedPages,
                pageCounts[orderedVolume.id])
            assertEquals("增量卷权威页数未写入", additionalExpected,
                pageCounts[additionalVolume.id])
            val files = chapterDir.listFiles().orEmpty().filter { it.isFile &&
                it.name.matches(Regex("\\d+\\.(jpg|jpeg|png|webp)", RegexOption.IGNORE_CASE))
            }.sortedBy { it.name.substringBefore('.').toIntOrNull() ?: Int.MAX_VALUE }
            assertEquals("磁盘页数与卷清单不符", expectedPages, files.size)
            files.forEachIndexed { index, file ->
                assertTrue("第 ${index + 1} 页大小异常: ${file.length()}", file.length() > 1000L)
                val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                BitmapFactory.decodeFile(file.absolutePath, bounds)
                assertTrue("第 ${index + 1} 页不能解码", bounds.outWidth > 0 && bounds.outHeight > 0)
            }
            val localDetail = gateway.httpText(ep.port,
                "/api/manga/copymanga/$comicId?catalog=local")
            assertTrue("本地目录详情失败 HTTP ${localDetail.code}", localDetail.ok)
            val local = EngineData.mangaDetail(localDetail.body)
                ?: throw AssertionError("本地目录详情不能解析")
            assertTrue("本地目录没有包含下载完成的卷",
                local.readingChapters.any { it.id == orderedVolume.id })
            assertTrue("本地目录没有包含增量下载的新卷",
                local.readingChapters.any { it.id == additionalVolume.id })
            assertTrue("本地目录未将卷排序在单话前", local.readingChapters
                .take(detail.volumes.size).all { it in detail.volumes })
            val localPages = gateway.httpText(ep.port,
                "/api/manga/copymanga/$comicId/chapter/${enc(orderedVolume.id)}")
            assertTrue("离线卷图片路由失败 HTTP ${localPages.code}", localPages.ok)
            val routed = EngineData.mangaChapterPages(localPages.body)
                ?: throw AssertionError("离线卷图片响应不能解析")
            assertTrue("离线阅读没有走本地图片", routed.local)
            assertEquals("离线阅读页数错误", expectedPages, routed.images.size)
            val incrementalPagesResponse = gateway.httpText(ep.port,
                "/api/manga/copymanga/$comicId/chapter/${enc(additionalVolume.id)}")
            assertTrue("增量卷离线路由失败 HTTP ${incrementalPagesResponse.code}",
                incrementalPagesResponse.ok)
            val incrementalPages = EngineData.mangaChapterPages(incrementalPagesResponse.body)
                ?: throw AssertionError("增量卷离线图片目录无法解析")
            assertTrue("增量卷阅读未走本地图片", incrementalPages.local)
            assertEquals("增量卷离线页数错误", additionalExpected, incrementalPages.images.size)
            for (index in routed.images.indices) {
                val imageResponse = gateway.httpText(ep.port,
                    "/api/manga/copymanga/$comicId/chapter/${enc(orderedVolume.id)}/img/$index")
                assertTrue("本地阅读第 ${index + 1} 页请求失败 HTTP ${imageResponse.code}",
                    imageResponse.ok)
                val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                BitmapFactory.decodeFile(files[index].absolutePath, bounds)
                assertTrue("本地阅读第 ${index + 1} 页无效", bounds.outWidth > 0)
            }
            println("COPYMANGA_FULL_VOLUME_PASS id=$comicId volume=${orderedVolume.name}+" +
                "${additionalVolume.name} pages=$expectedPages+$additionalExpected " +
                "bytes=${(files + additionalFiles).sumOf { it.length()}} " +
                "elapsedMs=${System.currentTimeMillis() - startedAt} " +
                "status=${task.optString("status")}")
        } finally {
            if (downloadStarted) {
                val response = gateway.httpText(ep.port,
                    "/api/manga/download/status?source=copymanga&cid=$comicId")
                var finalStatus = if (response.ok) {
                    JSONObject(response.body).optString("status")
                } else "unknown"
                if (response.ok && JSONObject(response.body).optString("status") in
                    setOf("running", "queued")) {
                    gateway.httpPost(ep.port,
                        "/api/manga/download/pause?source=copymanga&cid=$comicId", "{}")
                    val pauseDeadline = System.currentTimeMillis() + 30_000
                    while (System.currentTimeMillis() < pauseDeadline) {
                        val current = gateway.httpText(ep.port,
                            "/api/manga/download/status?source=copymanga&cid=$comicId")
                        if (!current.ok) {
                            finalStatus = "unknown"
                            break
                        }
                        finalStatus = JSONObject(current.body).optString("status")
                        if (finalStatus !in setOf("running", "queued")) break
                        kotlinx.coroutines.delay(200)
                    }
                }
                check(finalStatus !in setOf("running", "queued", "unknown")) {
                    "下载 worker 未确认停止，拒绝删除卷目录以避免写入竞态"
                }
            }
            // Remove the exact downloaded volume only; preserve any pre-existing siblings.
            if (chapterDir.exists()) chapterDir.deleteRecursively()
            if (additionalChapterDir.exists()) additionalChapterDir.deleteRecursively()
            val currentChildren = comicRoot.listFiles().orEmpty().toSet()
            (currentChildren - beforeRootChildren).forEach { it.deleteRecursively() }
            beforeRootFiles.forEach { (file, bytes) -> file.writeBytes(bytes) }
            if (!comicRootExisted && comicRoot.isDirectory && comicRoot.listFiles().isNullOrEmpty()) {
                comicRoot.delete()
            }
            if (comicCacheRoot.exists()) comicCacheRoot.deleteRecursively()
            if (comicCacheExisted) {
                comicCacheRoot.mkdirs()
                beforeCacheFiles.forEach { (file, bytes) ->
                    file.parentFile?.mkdirs()
                    file.writeBytes(bytes)
                }
            }
            snapshots.forEach { (file, bytes) ->
                if (bytes == null) file.delete()
                else {
                    file.parentFile?.mkdirs()
                    file.writeBytes(bytes)
                }
                check(if (bytes == null) !file.exists()
                    else file.isFile && file.readBytes().contentEquals(bytes)) {
                    "恢复测试前状态校验失败: ${file.name}"
                }
            }
            beforeRootFiles.forEach { (file, bytes) ->
                check(file.isFile && file.readBytes().contentEquals(bytes)) {
                    "恢复既有漫画清单失败: ${file.name}"
                }
            }
            beforeCacheFiles.forEach { (file, bytes) ->
                check(file.isFile && file.readBytes().contentEquals(bytes)) {
                    "恢复既有章节缓存失败: ${file.name}"
                }
            }
        }
    }

}
