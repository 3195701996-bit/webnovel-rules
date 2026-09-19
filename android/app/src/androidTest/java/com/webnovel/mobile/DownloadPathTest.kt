package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 下载队列的控制契约验收（不启动任何真实下载，因此不产生源站请求与磁盘写入）：
 *
 *   - GET /api/tasks 能取到队列（小说任务与漫画下载同一列表）；
 *   - 漫画的暂停/继续用**查询参数** source+cid（写进 body 是常见错误，这里锁住契约）；
 *   - 缺少参数必须明确失败，不能返回 ok 让人以为"操作成功"；
 *   - 批量暂停/继续返回的条数如实呈现（不假装操作了 N 个）；
 *   - 自检漫画不应该出现在任务列表里（证明测试没有偷偷起下载）。
 */
@RunWith(AndroidJUnit4::class)
class DownloadPathTest {

    private lateinit var comic: SelfTestComic
    private lateinit var gateway: EngineGateway

    private fun ev(line: String) = println("DOWNLOAD_PATH_EVIDENCE $line")

    @Before
    fun setUp() {
        comic = SelfTestComic()
        comic.create()
        gateway = EngineGateway(InstrumentationRegistry.getInstrumentation().targetContext)
        val state = runBlocking { gateway.connect() }
        assertTrue("引擎未就绪：$state", state is EngineState.Ready)
    }

    @After
    fun tearDown() {
        runCatching { runBlocking { gateway.stopEngine() } }
        comic.cleanup()
        assertFalse("自检漫画未清理", comic.exists())
    }

    @Test
    fun downloadQueue_contract() = runBlocking {
        val ep = gateway.currentEndpoint()!!

        // 1) 队列接口可用，且能解析出条目（可能为空：设备上没有任务）
        val r = gateway.httpText(ep.port, "/api/tasks")
        assertTrue("任务接口状态码=${r.code}", r.ok)
        val tasks = EngineData.tasks(r.body)
        ev("队列条目=${tasks.size} 进行中=${tasks.count { it.running }}")
        assertTrue("自检漫画不应出现在任务里（测试不得启动真实下载）",
            tasks.none { it.mangaComicId == comic.comicIdValue })

        // 2) 批量暂停/继续：返回的条数是数字且接口成功
        val pa = gateway.httpPost(ep.port, "/api/manga/download/pause-all")
        assertTrue("批量暂停应成功：HTTP ${pa.code}", pa.ok)
        val pausedN = JSONObject(pa.body).optInt("paused", -1)
        assertTrue("批量暂停应返回条数，实际=${pa.body.take(80)}", pausedN >= 0)
        val ra = gateway.httpPost(ep.port, "/api/manga/download/resume-all")
        assertTrue("批量继续应成功：HTTP ${ra.code}", ra.ok)
        val resumedN = JSONObject(ra.body).optInt("resumed", -1)
        assertTrue("批量继续应返回条数，实际=${ra.body.take(80)}", resumedN >= 0)
        ev("批量：paused=$pausedN resumed=$resumedN")

        // 3) 单条暂停/继续用查询参数（无对应任务时应成功空操作）
        val single = "/api/manga/download/pause?source=${comic.sourceKey}&cid=${comic.comicIdValue}"
        val ps = gateway.httpPost(ep.port, single)
        assertTrue("按 source+cid 暂停应为成功空操作：HTTP ${ps.code}", ps.ok)
        val rs = gateway.httpPost(ep.port,
            "/api/manga/download/resume?source=${comic.sourceKey}&cid=${comic.comicIdValue}")
        assertTrue("按 source+cid 继续应为成功空操作：HTTP ${rs.code}", rs.ok)
        ev("单条暂停/继续（查询参数）：${ps.code}/${rs.code}")

        // 4) 缺参数必须明确失败（不能静默 ok）
        val noParam = gateway.httpPost(ep.port, "/api/manga/download/pause")
        assertFalse("缺少 source/cid 不应返回 2xx", noParam.ok)
        ev("缺参数暂停被拒：HTTP ${noParam.code}")

        // 5) 小说任务：不存在的任务 id 必须明确失败
        val bogus = gateway.httpPost(ep.port, "/api/tasks/不存在的任务/pause")
        assertFalse("不存在的任务不应返回 2xx", bogus.ok)
        ev("不存在的小说任务被拒：HTTP ${bogus.code}")

        // 6) 队列条目状态文案：TaskItem.label 覆盖已知状态，避免界面出现空白
        val sample = EngineData.TaskItem(
            id = "x", type = "manga", title = "t", status = "running", running = true,
            done = 3, total = 10, current = "第 3 话", speed = 1.5, eta = 4.0,
            error = "", mangaKey = "mangadex:abc",
        )
        assertTrue("进行中应显示计数", sample.label.contains("3/10"))
        assertTrue("应显示速度", sample.speedLabel.contains("1.5"))
        assertTrue("漫画 key 应能拆出源", sample.mangaSource == "mangadex")
        assertTrue("漫画 key 应能拆出 id", sample.mangaComicId == "abc")
        assertTrue("完成状态应有文案", EngineData.TaskItem(
            "x", "manga", "t", "done", false, 10, 10, "", 0.0, 0.0, "", "s:c").label.contains("已完成"))
        assertTrue("失败状态应带原因", EngineData.TaskItem(
            "x", "manga", "t", "error", false, 0, 0, "", 0.0, 0.0, "网络不可用", "s:c")
            .label.contains("网络不可用"))
    }
}
