package com.webnovel.mobile

import android.net.Uri
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * 导出链路验收（诊断 §4 下载页：必须走系统文件选择与授权流程）。
 *
 * 这里验证的是**导出实现本身**：把本机引擎的导出流写进目标 URI，
 * 内容与字节数都经真实引擎产生，命中本地已下载数据、不请求源站。
 * 系统文件选择器是系统 UI，无法在仪器化测试里代替用户点选，
 * 因此选择器只验证"取消时不谎报成功"这一条分支。
 */
@RunWith(AndroidJUnit4::class)
class ExportPathTest {

    private lateinit var book: SelfTestBook
    private lateinit var comic: SelfTestComic
    private lateinit var gateway: EngineGateway
    private val ctx = InstrumentationRegistry.getInstrumentation().targetContext

    private fun ev(line: String) = println("EXPORT_PATH_EVIDENCE $line")

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
    fun export_novelTxt_and_mangaZip() = runBlocking {
        val ep = gateway.currentEndpoint()!!

        // 1) 小说全文导出：写入目标 URI（SAF 选定的位置在测试里用应用私有文件代替）
        val txt = File(ctx.cacheDir, "export-selftest.txt")
        if (txt.exists()) txt.delete()
        val r1 = Export.streamToUri(ctx, ep.port, ep.token,
            "/api/books/${book.bookKey}/txt", Uri.fromFile(txt))
        assertTrue("导出的字节数应为正数", r1.bytes > 0)
        assertTrue("目标文件不存在", txt.isFile)
        assertEquals("写入字节数与返回值不一致", r1.bytes, txt.length())
        val text = txt.readText(Charsets.UTF_8)
        // 说明：这本自检书没有对应书源，服务端走"本地合并"路径，导出内容只有章节正文，
        // 不带书名页眉（与桌面端同路径行为一致）。这里断言**实际内容**，不假设有页眉。
        assertTrue("导出内容应包含章节名", text.contains(SelfTestBook.CHAPTER1_NAME))
        assertTrue("导出内容应包含第一段正文", text.contains(SelfTestBook.CHAPTER1_BODY_1))
        assertTrue("导出内容应包含第二段正文", text.contains(SelfTestBook.CHAPTER1_BODY_2))
        assertTrue("未下载章节不应出现在导出里", !text.contains(SelfTestBook.CHAPTER2_NAME))
        ev("小说 TXT：bytes=${r1.bytes} 含章节名/两段正文=true 未下载章节未混入=true " +
            "警告=${r1.warning ?: "无"}")

        // 2) 失败必须失败：不存在的书 → 明确报 HTTP 404，不产生"成功"结果
        val bad = File(ctx.cacheDir, "export-should-not-exist.txt")
        if (bad.exists()) bad.delete()
        var err: Throwable? = null
        try {
            Export.streamToUri(ctx, ep.port, ep.token,
                "/api/books/${SelfTestBook.BOOK_NAME}_不存在/txt", Uri.fromFile(bad))
        } catch (t: Throwable) {
            err = t
        }
        assertTrue("导出不存在的书应当抛错", err != null)
        assertTrue("错误信息应带状态码，实际=${err?.message}",
            (err?.message ?: "").contains("404"))
        assertFalse("失败时不应留下非空文件", bad.exists() && bad.length() > 0)
        ev("失败分支：${err?.message?.take(60)}")

        // 3) 漫画 ZIP 导出：服务端按本地已下载图片打包
        val zip = File(ctx.cacheDir, "export-selftest.zip")
        if (zip.exists()) zip.delete()
        val r2 = Export.streamToUri(ctx, ep.port, ep.token,
            "/api/manga/${comic.sourceKey}/${comic.comicIdValue}/zip", Uri.fromFile(zip))
        assertTrue("ZIP 字节数应为正数", r2.bytes > 0)
        val head = zip.readBytes().take(2).toByteArray().toString(Charsets.US_ASCII)
        assertEquals("ZIP 文件头应为 PK", "PK", head)
        // 包内应含本地图片（按章节目录组织）
        java.util.zip.ZipFile(zip).use { zf ->
            val names = zf.entries().toList().map { it.name }
            assertTrue("压缩包内应有图片：$names",
                names.any { it.endsWith(".jpg") || it.endsWith(".webp") || it.endsWith(".png") })
            assertTrue("压缩包应按章节目录组织：$names",
                names.any { it.contains(SelfTestComic.CH1_ID) })
            ev("漫画 ZIP：bytes=${r2.bytes} 条目=${names.size} 例=${names.firstOrNull()}")
        }

        // 4) 文件名清洗：路径分隔符等必须被替换，避免 SAF 拒绝
        assertEquals("a_b_c", Export.safeFileName("a/b:c", "x"))
        assertTrue("超长名应被截断", Export.safeFileName("字".repeat(200), "x").length <= 80)
        assertEquals("空名用兜底", "book", Export.safeFileName("   ", "book"))
        ev("文件名清洗：a/b:c → ${Export.safeFileName("a/b:c", "x")}")
    }
}
