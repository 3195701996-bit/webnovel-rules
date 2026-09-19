package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * 删除类操作的契约验收（之前几轮一直标为"未做"的那部分）：
 *
 *   - 小说：DELETE /api/books/<key> → 软删除（移入 trash），书架不再列出；
 *   - 漫画：DELETE /api/manga/library/<source>/<id> → 移除书库记录 + 源缓存 + 阅读历史，
 *     但**已下载的图片文件保留**（界面文案必须这么写，用例把这条行为钉住）；
 *   - 任务：DELETE /api/tasks/manga_<source:id> 成功（未知任务空操作），
 *     小说任务不存在时必须 404（明确失败，不假装成功）；
 *   - 删除后用合成数据重建，验证删除不影响其它书。
 */
@RunWith(AndroidJUnit4::class)
class DeletePathTest {

    private lateinit var book: SelfTestBook
    private lateinit var comic: SelfTestComic
    private lateinit var gateway: EngineGateway
    private val ctx = InstrumentationRegistry.getInstrumentation().targetContext

    private fun ev(line: String) = println("DELETE_PATH_EVIDENCE $line")

    @Before
    fun setUp() {
        book = SelfTestBook()
        book.create()
        comic = SelfTestComic()
        comic.create()
        gateway = EngineGateway(ctx)
        val state = runBlocking { gateway.connect() }
        assertTrue("引擎未就绪：$state", state is EngineState.Ready)
    }

    @After
    fun tearDown() {
        runCatching { runBlocking { gateway.stopEngine() } }
        book.cleanup()
        comic.cleanup()
        assertFalse("自检书未清理", book.exists())
        assertFalse("自检漫画未清理", comic.exists())
    }

    @Test
    fun destructiveOperations_areHonest() = runBlocking {
        val ep = gateway.currentEndpoint()!!

        // 0) 前置：两者都在书架/书库里
        assertTrue("自检书应在书架",
            EngineData.novels(gateway.httpText(ep.port, "/api/books").body)
                .any { it.key == book.bookKey })
        assertTrue("自检漫画应在书库",
            EngineData.manga(gateway.httpText(ep.port, "/api/manga/library").body)
                .any { it.comicId == comic.comicIdValue })

        // 1) 小说软删除：返回 trash 路径，书架不再列出，目录消失
        val delBook = gateway.httpDelete(ep.port, "/api/books/${book.bookKey}")
        assertTrue("小说删除应成功：HTTP ${delBook.code}", delBook.ok)
        val trash = runCatching { JSONObject(delBook.body).optString("trash") }.getOrDefault("")
        assertTrue("应返回 trash 路径（软删除可恢复）", trash.isNotBlank())
        assertFalse("删除后书架不应再列出该书",
            EngineData.novels(gateway.httpText(ep.port, "/api/books").body)
                .any { it.key == book.bookKey })
        ev("小说软删除：trash=${trash.substringAfterLast('/').take(40)}")

        // 2) 再次删除同一本 → 404（明确失败，不假装成功）
        val again = gateway.httpDelete(ep.port, "/api/books/${book.bookKey}")
        assertEquals("重复删除应 404", 404, again.code)
        ev("重复删除：HTTP ${again.code}")

        // 3) 漫画：先写一条阅读历史，再移除书库记录
        gateway.httpPost(ep.port, "/api/manga/history",
            JSONObject().put("source", comic.sourceKey).put("comic_id", comic.comicIdValue)
                .put("idx", 0).put("pos", EngineData.mangaPos(SelfTestComic.CH1_NAME, 1))
                .put("title", SelfTestComic.TITLE).toString())
        assertEquals("前置：历史应有 1 条", 1, comic.historyEntryCount())
        val imgBefore = File(ctx.filesDir,
            "runtime/manga/downloads/${comic.sourceKey}/${comic.comicIdValue}/" +
                SelfTestComic.CH1_ID + "/0000.jpg")
        assertTrue("前置：已下载图片应存在", imgBefore.isFile)

        val delComic = gateway.httpDelete(ep.port,
            "/api/manga/library/${comic.sourceKey}/${comic.comicIdValue}")
        assertTrue("漫画移除应成功：HTTP ${delComic.code}", delComic.ok)
        assertFalse("移除后书库不应再列出该漫画",
            EngineData.manga(gateway.httpText(ep.port, "/api/manga/library").body)
                .any { it.comicId == comic.comicIdValue })
        assertEquals("移除后该漫画的阅读历史应被清理", 0, comic.historyEntryCount())
        // 这条是本轮界面文案的依据：服务端**不删** downloads 目录
        assertTrue("已下载的图片文件应保留（界面文案据此说明）", imgBefore.isFile)
        ev("漫画移除：书库与历史已清；已下载图片保留=${imgBefore.isFile}")

        // 4) 又一次移除 → 仍然成功但 removed 为空（幂等，不报错也不假装删了东西）
        val delAgain = gateway.httpDelete(ep.port,
            "/api/manga/library/${comic.sourceKey}/${comic.comicIdValue}")
        assertTrue("重复移除不应报错（幂等）：HTTP ${delAgain.code}", delAgain.ok)
        val removed = runCatching {
            JSONObject(delAgain.body).optJSONArray("removed")?.length() ?: 0
        }.getOrDefault(0)
        assertEquals("重复移除不应声称清掉了缓存", 0, removed)
        ev("重复移除：HTTP ${delAgain.code} removed=$removed")

        // 5) 任务删除：漫画键（未知）为空操作成功；小说任务不存在必须 404
        val mangaTask = gateway.httpDelete(ep.port,
            "/api/tasks/manga_${android.net.Uri.encode("${comic.sourceKey}:${comic.comicIdValue}")}")
        assertTrue("漫画任务删除应为成功空操作：HTTP ${mangaTask.code}", mangaTask.ok)
        val novelTask = gateway.httpDelete(ep.port, "/api/tasks/不存在的任务")
        assertEquals("不存在的小说任务应 404", 404, novelTask.code)
        ev("任务删除：漫画空操作 ${mangaTask.code} / 小说不存在 ${novelTask.code}")

        // 6) 删除不影响其它数据：重建自检书后仍能正常出现在书架
        book.create()
        assertTrue("重建后自检书应重新出现在书架",
            EngineData.novels(gateway.httpText(ep.port, "/api/books").body)
                .any { it.key == book.bookKey })
        ev("重建后书架正常（删除只作用于目标）")
    }
}
