package com.webnovel.mobile

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * P1-A 漫画闭环验收（真机、真实源站）：
 *   **搜索 → 详情 → 下载 → 关掉引擎 → 离线打开并读到本地图片**。
 *
 * 方向基线 §7.2 的 P1-A 要求"从真实首装路径完成搜索、阅读、收藏、退出续读、下载后
 * 断网阅读，不借网页补步骤"。本用例把这条链路整条走一遍，用**真实源**（MangaDex，
 * 本机实测通过的源）而不是夹具——夹具只用于离线段的渲染，不能证明源站可用。
 *
 * 断言口径：
 *   · 搜索走**流式端点**（与 App 同一条路径），首屏必须有结果；
 *   · 详情必须来自原生接口；
 *   · 下载后本地必须真出现**图片文件**（用图片魔数判定，不看接口自说自话）；
 *   · **停掉引擎**后，离线索引仍能找到该话与图片（这就是"断网可读"的证据）；
 *   · 全程不得依赖网页（不加载桌面页）。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class MangaClosedLoopTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private lateinit var gateway: EngineGateway
    private val source = "mangadex"
    private var comicId: String = ""
    private var chapterId: String = ""
    private var created = false

    private fun ev(line: String) = println("MANGA_LOOP_EVIDENCE $line")

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Before
    fun setUp() {
        gateway = EngineGateway(ctx)
        val state = runBlocking { gateway.connect() }
        assertTrue("引擎未就绪：$state", state is EngineState.Ready)
    }

    @After
    fun tearDown() {
        runCatching {
            runBlocking {
                // 用例体内会**主动停引擎**来证明离线可读；清理必须在引擎可用时做，
                // 否则 DELETE 打在已停的引擎上静默失败，设备上会留下测试下载
                // （实测踩到：83 张图 + 书库条目残留）。
                var ep = gateway.currentEndpoint()
                if (ep == null) {
                    val st = gateway.connect()
                    ep = (st as? EngineState.Ready)?.endpoint
                }
                if (created && ep != null && comicId.isNotBlank()) {
                    val r = gateway.httpDelete(ep.port,
                        "/api/manga/library/$source/$comicId?files=1")
                    ev("清理：移除书库条目并删除下载文件 HTTP ${r.code}")
                }
                gateway.stopEngine()
            }
        }
    }

    /** 断言失败时带自定义消息的 assertEquals（JUnit 的 assertEquals(String,..) 语义易混） */
    private fun assertEqualsMsg(msg: String, expected: Any?, actual: Any?) =
        org.junit.Assert.assertEquals(msg, expected, actual)

    /** 按字节取回本机引擎响应（图片必须按字节校验/解码，不能读成字符串） */
    private fun httpBytes(port: Int, path: String): ByteArray? {
        val ep = gateway.currentEndpoint() ?: return null
        val url = java.net.URL("http://127.0.0.1:$port$path")
        val c = url.openConnection() as java.net.HttpURLConnection
        return try {
            c.requestMethod = "GET"
            c.connectTimeout = 8000
            c.readTimeout = 60_000
            c.setRequestProperty("Connection", "close")
            if (ep.token.isNotEmpty()) c.setRequestProperty("X-Mobile-Token", ep.token)
            if (c.responseCode !in 200..299) null
            else c.inputStream.use { it.readBytes() }
        } finally {
            runCatching { c.disconnect() }
        }
    }

    private fun isImage(f: File): Boolean {
        if (!f.isFile || f.length() < 1000) return false
        val head = ByteArray(12)
        return try {
            f.inputStream().use { it.read(head) }
            val b = head
            (b[0] == 0xFF.toByte() && b[1] == 0xD8.toByte()) ||          // jpeg
                (b[0] == 0x89.toByte() && b[1] == 'P'.code.toByte()) ||   // png
                (String(b, 0, 4) == "RIFF") ||                            // webp
                (String(b, 0, 3) == "GIF")
        } catch (t: Throwable) {
            false
        }
    }

    @Test
    // 显式声明返回 Unit：runBlocking 的最后一句是 gateway.stopEngine()（有返回值），
    // 不写 Unit 会让测试方法的返回类型推断成非 void → JUnit 直接拒绝类
    // （实测报 InvalidTestClassError: Method ... should be void）
    fun firstRunLoop_searchDownloadThenOfflineRead(): Unit = runBlocking {
        val ep = gateway.currentEndpoint()!!

        // 1) 搜索：与 App 同一条流式路径，首屏必须有结果
        val hits = ArrayList<MangaSearchHit>()
        val t0 = System.currentTimeMillis()
        var firstMs = 0L
        val code = gateway.streamEvents(ep.port,
            "/api/manga/search/stream?q=" + java.net.URLEncoder.encode("巨人", "UTF-8")) { evt ->
            val groups = evt.optJSONArray("groups") ?: return@streamEvents
            if (groups.length() == 0) return@streamEvents
            val (h, _) = EngineData.mangaSearch(
                JSONObject().put("results", groups).toString())
            for (x in h) {
                if (hits.none { it.source == x.source && it.comicId == x.comicId }) {
                    hits.add(x)
                }
            }
            if (firstMs == 0L) firstMs = System.currentTimeMillis() - t0
        }
        assertTrue("流式搜索应成功：code=$code", code in 200..299)
        assertTrue("首次搜索必须在 12s 内出结果（实际 ${System.currentTimeMillis() - t0}ms，" +
            "首条 ${firstMs}ms）", firstMs in 1..12_000)
        ev("搜索：${hits.size} 条，首条 ${firstMs}ms")

        // 目标作品用夹具解析（缓存 + 限源搜索 + 退避），**不再拿搜索首条**：
        // 整包设备运行实测踩到——MangaDex 抖动时首位可能是 baozi（章节 JS 渲染，
        // 已知缺口），详情直接 502，闭环就断在与被测链路无关的地方。
        // 上面的流式搜索断言保留：它验的是"搜索能出结果且够快"，与选谁下载是两件事。
        assertTrue("流式搜索必须至少返回一条结果（实际 ${hits.size}）", hits.isNotEmpty())
        val fx = MangaFixture.pick(gateway, ep.port, source) { ev(it) }
        comicId = fx.comicId
        ev("选中的作品（夹具）：${fx.source} / ${fx.comicId}")

        // 2) 详情（原生接口）
        val dr = gateway.httpText(ep.port, "/api/manga/${fx.source}/$comicId")
        assertTrue("详情应 200：HTTP ${dr.code}", dr.ok)
        val detail = EngineData.mangaDetail(dr.body)
            ?: throw AssertionError("详情解析失败")
        assertTrue("详情必须有章节", detail.chapters.isNotEmpty())
        val chapter = detail.chapters.firstOrNull { it.id == fx.chapterId }
            ?: detail.chapters.last()             // 取最后一话：页数通常最少，下载快
        chapterId = chapter.id
        ev("详情：共 ${detail.chapters.size} 话，本次下载「${chapter.name.take(20)}」")

        // 3) 下载这一话（原生下载接口，与详情页按钮同一条）
        val body = JSONObject().put("title", detail.title)
            .put("cover", detail.cover)
            .put("chapters", JSONArray().put(chapter.id)).toString()
        val ok = gateway.httpPost(ep.port, "/api/manga/${fx.source}/$comicId/download", body)
        assertTrue("创建下载任务应 2xx：HTTP ${ok.code} ${ok.body.take(120)}",
            ok.code in 200..299)
        created = true

        // 3b) 预期页数（评审要求：不能只用"文件数稳定"当完成判据）
        val chPages = gateway.httpText(ep.port,
            "/api/manga/${fx.source}/${comicId}" +
                "/chapter/${android.net.Uri.encode(chapterId)}")
        val expectedPages = EngineData.mangaChapterPages(chPages.body)?.let { p ->
            if (p.local) 0 else p.images.size
        } ?: 0
        ev("服务端给出该话题图清单：HTTP ${chPages.code}，预期页数=${if (expectedPages > 0) expectedPages else "未知"}")

        // 4) 等本地真出现图片（不看接口自说自话，看磁盘）
        val chapterDir = File(OfflineStore.runtimeDir(ctx),
            "manga/downloads/${fx.source}/$comicId/$chapterId")
        var files: List<File> = emptyList()
        val dl0 = System.currentTimeMillis()
        var retries = 0
        for (i in 0 until 60) {
            delay(2000)
            files = chapterDir.listFiles().orEmpty().filter { isImage(it) }
            if (files.isNotEmpty()) break
            // 源站侧抖动（实测两种：一次返回 0 章、一次 "Remote end closed connection"）
            // 不是我们的链路问题；允许**最多重试 2 次**，每次都打印现场，
            // 既不掩盖问题，也不把外部抖动当成回归。：源站偶发"Remote end closed connection"（实测整包运行时
            // 命中过一次，单独复跑立刻通过）——这是源站侧抖动，不是链路缺陷；
            // 但**不掩盖**：每次重试都打印现场，超限仍按失败处理。
            if (retries < 2 && i >= 4 && i % 8 == 4) {
                val st = runCatching {
                    JSONObject(gateway.httpText(ep.port,
                        "/api/manga/download/status?source=${fx.source}&cid=$comicId").body)
                        .optString("status")
                }.getOrDefault("")
                if (st == "error") {
                    retries++
                    ev("下载在源站侧失败（status=error，第 $retries 次重试；" +
                        "原因见任务 error 字段）→ 重新建任务")
                    gateway.httpPost(ep.port, "/api/manga/${fx.source}/$comicId/download",
                        JSONObject().put("title", detail.title).put("cover", detail.cover)
                            .put("chapters", JSONArray().put(chapterId)).toString())
                    delay(3000)      // 给新任务一点启动时间再继续观察
                }
            }
        }
        assertTrue("下载后本地必须出现图片文件（等了 ${(System.currentTimeMillis() - dl0) / 1000}s，" +
            "目录=${chapterDir.exists()}）", files.isNotEmpty())
        // 等数量稳定（连续三次不变）再取快照：下载还在继续时取快照，
        // 停引擎后的数量必然更多，那不是产品问题而是测试自身的竞态（实测踩到）
        var stable = 0
        var lastN = files.size
        for (i in 0 until 45) {
            delay(2000)
            val n = chapterDir.listFiles().orEmpty().count { isImage(it) }
            if (n == lastN) {
                stable++
                if (stable >= 3) break
            } else {
                stable = 0; lastN = n
            }
        }
        files = chapterDir.listFiles().orEmpty().filter { isImage(it) }
        val snapshot = files.map { it.name }.toSet()
        ev("下载稳定：${files.size} 张图片落盘（共 ${(System.currentTimeMillis() - dl0) / 1000}s）")

        // 4b) 任务状态必须真的完成（不是"文件数不动了"就算完成）
        var taskStatus = ""
        for (i in 0 until 30) {
            val st = gateway.httpText(ep.port,
                "/api/manga/download/status?source=${fx.source}&cid=$comicId")
            taskStatus = runCatching { JSONObject(st.body).optString("status") }
                .getOrDefault("")
            if (taskStatus == "done" || taskStatus == "error") break
            delay(2000)
        }
        ev("下载任务状态=$taskStatus")
        assertEqualsMsg("下载任务应以 done 收尾（实际 $taskStatus）", "done", taskStatus)

        // 4c) 张数必须与源站清单一致（不能少、也不能多出别的图）
        if (expectedPages > 0) {
            assertEqualsMsg("落盘张数必须等于源站清单页数（预期 $expectedPages）",
                expectedPages, files.size)
            ev("张数与源站清单一致：$expectedPages 张")
        } else {
            ev("源站未给出页数清单，跳过张数一致性断言（如实记录，不假装核对过）")
        }

        // 4d) **真正解码**每张图（只看文件长度/魔数不等于能渲染）
        var decoded = 0
        var minW = Int.MAX_VALUE
        var minH = Int.MAX_VALUE
        for (f in files) {
            val o = android.graphics.BitmapFactory.Options().apply { inJustDecodeBounds = true }
            android.graphics.BitmapFactory.decodeFile(f.absolutePath, o)
            assertTrue("图片必须能解码出尺寸：${f.name}（${f.length()} 字节）",
                o.outWidth > 0 && o.outHeight > 0)
            decoded++
            minW = minOf(minW, o.outWidth)
            minH = minOf(minH, o.outHeight)
        }
        ev("逐张解码通过：$decoded 张（最小 ${minW}×${minH}）")

        // 5) 在线读一页：按**字节**取回并解码（评审要求：文本长度不能替代实际解码）
        val imgBytes = httpBytes(ep.port,
            "/api/manga/${fx.source}/$comicId/chapter/$chapterId/img/0")
        assertTrue("在线读图应有响应体（实际 ${imgBytes?.size ?: 0} 字节）",
            (imgBytes?.size ?: 0) > 1000)
        val bounds = android.graphics.BitmapFactory.Options().apply { inJustDecodeBounds = true }
        android.graphics.BitmapFactory.decodeByteArray(imgBytes, 0, imgBytes!!.size, bounds)
        assertTrue("在线取回的图片必须能解码（outs=${bounds.outWidth}x${bounds.outHeight}）",
            bounds.outWidth > 0 && bounds.outHeight > 0)
        ev("在线读图并解码成功：${imgBytes.size} 字节，${bounds.outWidth}×${bounds.outHeight}")

        // 6) **停引擎**，再证明"断网/离线可读"
        gateway.stopEngine()
        delay(1500)
        val offlineShelf = OfflineStore.manga(ctx)
        val found = offlineShelf.firstOrNull {
            it.source == fx.source && it.comicId == comicId
        }
        assertTrue("离线索引必须列出刚下载的漫画（实际 ${offlineShelf.size} 部）", found != null)
        val chapters = OfflineStore.mangaChapters(ctx, fx.source, comicId)
        assertTrue("离线索引必须能找到该话的图片", chapters.isNotEmpty())
        val offlineNames = chapters.first().second.map { it.name }.toSet()
        assertTrue("离线索引必须至少包含停引擎前已落盘的图片" +
            "（快照 ${snapshot.size} 张，离线 ${offlineNames.size} 张）",
            offlineNames.containsAll(snapshot))
        assertTrue("离线索引里的图片必须是真图片（魔数校验）",
            chapters.first().second.all { isImage(it) })
        ev("离线（引擎已停）：书架列出该漫画（${found!!.imageCount} 张图），" +
            "该话可读 ${chapters.first().second.size} 张")

        // 7) 全程没有网页参与（**视图层**证据：整个闭环里没有创建过 WebView）
        Views.assertNoWebView(rule.activity, "漫画闭环（搜索→详情→下载→离线）")
        ev("闭环完成：搜索→详情→下载→离线可读，未借助网页")

        // 8) 手机上必须**能真正释放空间**：重新拉起引擎 → 连文件一起移除 →
        //    断言目录真的消失（默认行为只移除记录、文件保留，手机端不够用）
        val back = gateway.connect()
        val ep2 = (back as? EngineState.Ready)?.endpoint
        assertTrue("清理阶段引擎应能重新就绪：$back", ep2 != null)
        val comicDir = File(OfflineStore.runtimeDir(ctx),
            "manga/downloads/${fx.source}/$comicId")
        val sizeBefore = comicDir.walkTopDown().filter { it.isFile }
            .sumOf { it.length() }
        val rm = gateway.httpDelete(ep2!!.port,
            "/api/manga/library/${fx.source}/$comicId?files=1")
        assertTrue("移除并删文件应 200：HTTP ${rm.code}", rm.ok)
        val freed = runCatching { JSONObject(rm.body).optLong("freed_bytes") }.getOrDefault(0L)
        assertTrue("已下载目录必须被真正删除（释放 ${freed} 字节）", !comicDir.exists())
        assertTrue("释放字节数应与删除前一致（删前 ${sizeBefore}）", freed >= sizeBefore)
        created = false          // 已在用例内清理完，tearDown 无需再删
        ev("释放空间：删除下载目录成功，释放 ${freed / 1024} KB")
        gateway.stopEngine()
    }
}
