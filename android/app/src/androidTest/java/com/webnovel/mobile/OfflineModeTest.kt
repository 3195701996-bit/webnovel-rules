package com.webnovel.mobile

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.UUID

/**
 * 离线入口验收（方向基线 §5.4 / §6.6）：**引擎不可用时也要能看书架与已下载内容**。
 *
 * 过去只有引擎 ready 才显示主框架，错误页却承诺"已下载内容仍可离线阅读"——
 * 用户根本进不去书架，这就是入口承诺不一致。本用例直接以 `EngineState.Failed`
 * 渲染应用外壳（不真把引擎弄挂，也不依赖网络），验证：
 *   1. 主框架仍然渲染（页签在），并出现诚实的引擎横幅（含重试/诊断入口）；
 *   2. 书架退化为**本地只读索引**，列出已下载的小说与漫画（用真实目录结构的夹具）；
 *   3. 离线小说阅读器能读到缓存正文（不经过引擎）；
 *   4. 离线漫画阅读器能列出本地图片；
 *   5. 离线设置里"阅读设置 / 产物信息"可用，且不谎称其余设置可用。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class OfflineModeTest {

    @get:Rule
    val rule = createComposeRule()

    private lateinit var book: SelfTestBook
    private lateinit var comic: SelfTestComic

    private fun ev(line: String) = println("OFFLINE_MODE_EVIDENCE $line")

    private fun texts(text: String, substring: Boolean = false) =
        rule.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().size

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size

    private fun waitTag(tag: String, timeoutMs: Long = 20_000) {
        rule.waitUntil(timeoutMs) { nodes(tag) > 0 }
    }

    @Before
    fun setUp() {
        book = SelfTestBook()
        book.create()
        comic = SelfTestComic()
        comic.create()
        ev("已造离线夹具：1 本小说（1/3 章有正文）+ 1 部漫画（本地图片）")
    }

    @After
    fun tearDown() {
        comic.cleanup()
        book.cleanup()
        assertTrue("自检书未清理干净", !book.exists())
        assertTrue("自检漫画未清理干净", !comic.exists())
        ev("清理完成")
    }

    /**
     * 启动中 != 失败：正常启动时**不能**挂"本机引擎未就绪 + 重试"（会吓用户，
     * 也让"未就绪"失去意义）。这条是被冷启动验收用例抓出来后补的回归。
     */
    @Test
    fun engineStarting_isNotReportedAsFailure() {
        val ctx = InstrumentationRegistry.getInstrumentation().targetContext
        rule.setContent {
            AppShell(
                gateway = EngineGateway(ctx),
                engine = EngineState.Starting,
                stage = "正在校验引擎实例",
                onRetry = {},
            )
        }
        rule.waitUntil(20_000) {
            rule.onAllNodesWithText("正在校验引擎实例", substring = true)
                .fetchSemanticsNodes().isNotEmpty()
        }
        assertTrue("启动中必须显示进度文案",
            rule.onAllNodesWithText("正在校验引擎实例", substring = true)
                .fetchSemanticsNodes().isNotEmpty())
        assertTrue("启动中不得显示失败横幅",
            rule.onAllNodesWithText("本机引擎未就绪").fetchSemanticsNodes().isEmpty())
        assertTrue("启动中不得显示离线书架",
            rule.onAllNodesWithTag("offline_shelf").fetchSemanticsNodes().isEmpty())
        assertTrue("启动中不得给出重试按钮",
            rule.onAllNodesWithTag("engine_banner_retry").fetchSemanticsNodes().isEmpty())
        ev("启动中：只显示进度，不谎称未就绪、不显示离线书架")
    }

    @Test
    fun engineDown_stillReadsDownloadedContent() {
        val ctx = InstrumentationRegistry.getInstrumentation().targetContext
        val gateway = EngineGateway(ctx)

        // 以"引擎失败"渲染应用外壳：这正是用户遇到的情形
        rule.setContent {
            AppShell(
                gateway = gateway,
                engine = EngineState.Failed("启动本机引擎", "自检：故意制造引擎不可用"),
                stage = "启动本机引擎",
                onRetry = {},
            )
        }

        // 1) 主框架仍在（页签 + 横幅），不再是"只有重试按钮"的死路
        waitTag("engine_banner")
        rule.onNodeWithTag("engine_banner").assertIsDisplayed()
        assertTrue("横幅必须给出重试入口", nodes("engine_banner_retry") > 0)
        assertTrue("横幅必须给出诊断入口", nodes("engine_banner_diag") > 0)
        assertTrue("页签必须仍在（书架）", texts("书架") > 0)
        ev("引擎不可用：主框架仍在，横幅含重试/诊断")

        // 2) 书架退化为本地只读索引：夹具里的书与漫画都在
        waitTag("offline_shelf")
        rule.onNodeWithTag("offline_shelf").assertIsDisplayed()
        assertTrue("离线书架必须列出已下载的小说",
            texts(SelfTestBook.BOOK_NAME, substring = true) > 0)
        assertTrue("离线书架必须列出已下载的漫画",
            texts(SelfTestComic.TITLE, substring = true) > 0)
        assertTrue("必须说明只列出已下载内容",
            texts("只列出", substring = true) > 0)
        ev("离线书架列出了已下载的小说与漫画，并说明范围")

        // 3) 打开离线小说阅读器：正文来自本地缓存
        rule.onAllNodesWithText(SelfTestBook.BOOK_NAME, substring = true)[0].performClick()
        waitTag("offline_novel_body")
        rule.onNodeWithTag("offline_novel_body").assertIsDisplayed()
        assertTrue("离线阅读器必须显示缓存正文",
            texts(SelfTestBook.CHAPTER1_BODY_1, substring = true) > 0)
        ev("离线小说阅读器：读到本地缓存正文（未经过引擎）")
        rule.onNodeWithContentDescription("返回").performClick()
        waitTag("offline_shelf")

        // 4) 打开离线漫画阅读器：图片来自本地文件
        rule.onAllNodesWithText(SelfTestComic.TITLE, substring = true)[0].performClick()
        waitTag("offline_manga_pages")
        rule.onNodeWithTag("offline_manga_pages").assertIsDisplayed()
        assertTrue("离线漫画阅读器必须显示页数说明",
            texts("张图片来自本机", substring = true) > 0)
        ev("离线漫画阅读器：图片来自本机文件")
        rule.onNodeWithContentDescription("返回").performClick()
        waitTag("offline_shelf")

        // 5) 离线设置：本地可用的部分在，且不谎称其余可用
        rule.onAllNodesWithText("设置")[0].performClick()
        waitTag("offline_settings")
        rule.onNodeWithTag("offline_reader_prefs").assertIsDisplayed()
        rule.onNodeWithTag("offline_build_info").assertIsDisplayed()
        // 文案里有两处提到"需要引擎"（说明行 + 入口标注）：断言"存在"而不是"唯一"
        assertTrue("必须如实标注其余设置需要引擎",
            texts("需要引擎", substring = true) >= 1)
        ev("离线设置：阅读设置/产物信息可用，其余如实标注需要引擎")

        // 6) 阅读设置在离线时也能打开（排版不该依赖引擎）
        rule.onNodeWithTag("offline_reader_prefs").performClick()
        rule.waitUntil(20_000) { texts("小说排版", substring = true) > 0 }
        ev("离线时阅读设置可打开")
    }

    @Test
    fun legacyOfflineMangaManifestUsesCanonicalOrderOnDevice() {
        val ctx = InstrumentationRegistry.getInstrumentation().targetContext
        val comicId = "__offline_order_${UUID.randomUUID().toString().replace("-", "") }__"
        val comicDir = File(OfflineStore.runtimeDir(ctx), "manga/downloads/selftest/$comicId")
        val rows = listOf(
            "ep-10" to "第10话",
            "vol-2" to "第二卷",
            "ep-2" to "第2話",
            "vol-1" to "第一卷",
            "vol-3" to "第三巻",
            "ep-1" to "第1话",
            "appendix" to "番外2",
        )
        try {
            rows.forEachIndexed { index, (id, _) ->
                val chapter = File(comicDir, id)
                assertTrue("创建章节测试目录失败", chapter.mkdirs())
                File(chapter, "0000.jpg").writeBytes(SelfTestComic.makeJpeg(index))
            }
            File(comicDir, "_info.json").writeText(
                JSONObject().put("chapters", JSONArray().apply {
                    rows.forEach { (id, name) ->
                        put(JSONObject().put("id", id).put("name", name))
                    }
                }).toString(),
                Charsets.UTF_8,
            )

            assertEquals(
                listOf("vol-1", "vol-2", "vol-3", "ep-1", "ep-2", "ep-10", "appendix"),
                OfflineStore.mangaChapters(ctx, "selftest", comicId).map { it.first },
            )
            ev("设备文件系统旧清单顺序：卷在前、话数自然序、番外置后")
        } finally {
            assertTrue("只清理本测试 UUID 专属漫画目录",
                comicDir.deleteRecursively() || !comicDir.exists())
        }
    }

    @Test
    fun copyMangaLegacyWebAliasRemainsReadableWhenEngineIsUnavailable() {
        val ctx = InstrumentationRegistry.getInstrumentation().targetContext
        val comicId = "__offline_copy_alias_${UUID.randomUUID().toString().replace("-", "")}__"
        val runtime = OfflineStore.runtimeDir(ctx)
        val canonicalRoot = File(runtime, "manga/downloads/copymanga")
        val webRoot = File(runtime, "manga/downloads/copymanga_web")
        val legacyCacheRoot = File(runtime, "manga/_cache/copymanga")
        val canonicalRootExisted = canonicalRoot.exists()
        val webRootExisted = webRoot.exists()
        val legacyCacheRootExisted = legacyCacheRoot.exists()
        val canonicalDir = File(runtime, "manga/downloads/copymanga/$comicId")
        val webDir = File(runtime, "manga/downloads/copymanga_web/$comicId")
        val legacyCacheDir = File(runtime, "manga/_cache/copymanga/$comicId")
        val jpeg = SelfTestComic.makeJpeg(17)
        try {
            val volume = File(canonicalDir, "volume-1").apply { mkdirs() }
            File(volume, "0000.jpg").writeBytes(jpeg)
            File(canonicalDir, "_info.json").writeText(
                JSONObject().put("chapters", JSONArray().put(
                    JSONObject().put("id", "volume-1").put("name", "第1卷"),
                )).toString(), Charsets.UTF_8,
            )
            // Legacy APP/Web adapters may persist distinct IDs for the same
            // chapter. The web alias has the more complete page set.
            val episode = File(webDir, "web-volume-id").apply { mkdirs() }
            File(episode, "0000.jpg").writeBytes(jpeg)
            File(episode, "0001.jpg").writeBytes(SelfTestComic.makeJpeg(18))
            File(webDir, "_info.json").writeText(
                JSONObject().put("chapters", JSONArray().put(
                    JSONObject().put("id", "web-volume-id").put("name", "第１卷"),
                )).toString(), Charsets.UTF_8,
            )
            File(legacyCacheDir, "legacy-episode").apply { mkdirs() }
                .resolve("0000.jpg").writeBytes(SelfTestComic.makeJpeg(19))
            File(legacyCacheDir, "ordinary-read-cache").apply { mkdirs() }
                .resolve("0000.jpg").writeBytes(SelfTestComic.makeJpeg(20))
            File(legacyCacheDir, "_info.json").writeText(
                JSONObject().put("chapters", JSONArray().put(
                    JSONObject().put("id", "legacy-episode").put("name", "第2话"),
                )).toString(), Charsets.UTF_8,
            )

            val localChapters = OfflineStore.mangaChapters(ctx, "copymanga", comicId)
            assertEquals("跨 APP/Web ID 的同卷去重，且清单证明的 cache-root 旧下载保留",
                listOf("volume-1", "legacy-episode"), localChapters.map { it.first })
            assertEquals("阅读单元应取图片更完整的 Web 别名副本", 2,
                localChapters.first { it.first == "volume-1" }.second.size)
            assertTrue("普通在线阅读 cache 不得被提升成下载章节",
                localChapters.none { row -> row.second.any { it.parentFile?.name == "ordinary-read-cache" } })
            assertEquals("离线书架章节数需与归并后的阅读目录一致", 2,
                OfflineStore.manga(ctx).single { it.comicId == comicId }.chapterCount)

            rule.setContent {
                AppShell(
                    gateway = EngineGateway(ctx),
                    engine = EngineState.Failed("启动本机引擎", "自检：验证旧下载别名"),
                    stage = "启动本机引擎",
                    onRetry = {},
                )
            }
            waitTag("offline_shelf")
            assertEquals("两个历史 source 根在离线书架中应归并为同一作品", 1,
                texts(comicId, substring = true))
            rule.onAllNodesWithText(comicId, substring = true)[0].performClick()
            waitTag("offline_manga_pages")
            assertTrue("去重后的完整别名副本必须通过纯本地路径打开",
                texts("2 张图片来自本机", substring = true) > 0)
            ev("CopyManga 历史 Web source 不同 ID 同卷去重并选完整副本，离线打开")
        } finally {
            assertTrue("只清理本轮 UUID 专属 canonical 夹具",
                canonicalDir.deleteRecursively() || !canonicalDir.exists())
            assertTrue("只清理本轮 UUID 专属 Web 别名夹具",
                webDir.deleteRecursively() || !webDir.exists())
            assertTrue("只清理本轮 UUID 专属 legacy cache 夹具",
                legacyCacheDir.deleteRecursively() || !legacyCacheDir.exists())
            if (!canonicalRootExisted) canonicalRoot
                .takeIf { it.listFiles()?.isEmpty() == true }?.delete()
            if (!webRootExisted) webRoot
                .takeIf { it.listFiles()?.isEmpty() == true }?.delete()
            if (!legacyCacheRootExisted) legacyCacheRoot
                .takeIf { it.listFiles()?.isEmpty() == true }?.delete()
        }
    }
}
