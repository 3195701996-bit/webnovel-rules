package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 阶段 D：后台与恢复的**数据面**验收（诊断 §8 优先级 3、设计 §6.2）。
 *
 * 这里验证"受限/重启后不丢数据、能恢复"这一半（另一半是前台服务在后台继续跑，
 * 由 adb 侧证据覆盖，见交付报告）：
 *
 *   1. 造书与漫画、写入阅读进度（相当于用户读到一半）；
 *   2. 停止引擎（模拟进程被杀 / 系统回收 / 用户停止）；
 *   3. 重新连接（新引擎实例）后：
 *      - 实例标识必须与上一次不同（确实重启了，不是复用旧进程）；
 *      - 书架里的小说与漫画仍在，阅读进度原样保留；
 *      - 队列接口可用，且没有"幽灵 running 任务"（中断任务被收敛为可恢复状态）。
 *
 * 用合成数据保证可重复，结束清理干净。
 */
@RunWith(AndroidJUnit4::class)
class EngineRecoveryPathTest {

    private lateinit var book: SelfTestBook
    private lateinit var comic: SelfTestComic
    private lateinit var gateway: EngineGateway
    private val ctx = InstrumentationRegistry.getInstrumentation().targetContext

    private fun ev(line: String) = println("RECOVERY_EVIDENCE $line")

    @Before
    fun setUp() {
        book = SelfTestBook()
        book.create()
        comic = SelfTestComic()
        comic.create()
        gateway = EngineGateway(ctx)
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
    fun stateSurvivesEngineRestart() = runBlocking {
        val s1 = gateway.connect()
        assertTrue("引擎未就绪：$s1", s1 is EngineState.Ready)
        val ep1 = (s1 as EngineState.Ready).endpoint
        ev("第一次启动 instance=${ep1.instanceId.take(8)} port=${ep1.port}")

        // 1) 写入阅读进度：相当于用户读到第 2 章 40%
        val save = gateway.httpPost(ep1.port, "/api/books/${SelfTestBook().bookKey}/progress",
            JSONObject().put("idx", 2).put("pct", 40)
                .put("name", SelfTestBook.CHAPTER2_NAME).toString())
        assertTrue("进度写入应成功：HTTP ${save.code}", save.ok)
        val mangaSave = gateway.httpPost(ep1.port, "/api/manga/history",
            JSONObject().put("source", comic.sourceKey).put("comic_id", comic.comicIdValue)
                .put("idx", 1).put("pos", EngineData.mangaPos(SelfTestComic.CH2_NAME, 2))
                .put("title", SelfTestComic.TITLE).toString())
        assertTrue("漫画进度写入应成功：HTTP ${mangaSave.code}", mangaSave.ok)
        ev("重启前：已写入小说进度与漫画进度")

        // 2) 停止引擎（模拟进程被杀/系统回收）
        val stopped = gateway.stopEngine()
        assertTrue("引擎应能停止", stopped)
        // 停止后再连必须重新走完整链路（这里正好验证"不是复用旧状态"）
        ev("已停止引擎（模拟被系统回收）")

        // 3) 重新连接：新实例
        val s2 = gateway.connect()
        assertTrue("重启后引擎未就绪：$s2", s2 is EngineState.Ready)
        val ep2 = (s2 as EngineState.Ready).endpoint
        assertNotEquals("重启后实例标识应不同（确认是新的引擎实例）",
            ep1.instanceId, ep2.instanceId)
        ev("第二次启动 instance=${ep2.instanceId.take(8)} port=${ep2.port}（与上次不同）")

        // 4) 小说：仍在书架，进度原样
        val novels = EngineData.novels(gateway.httpText(ep2.port, "/api/books").body)
        val mine = novels.firstOrNull { it.key == SelfTestBook().bookKey }
        assertTrue("重启后书架里应仍有自检书", mine != null)
        assertEquals("重启后章节总数", 3, mine!!.total)
        assertEquals("重启后读到第几章", 2, mine.readIdx)
        assertEquals("重启后章内百分比", 40, mine.readPct)
        assertEquals("重启后章名", SelfTestBook.CHAPTER2_NAME, mine.readName)
        ev("重启后小说：read_idx=${mine.readIdx} read_pct=${mine.readPct} read_name=${mine.readName}")

        // 详情页的"继续阅读"落点也应跟随
        val detail = EngineData.bookDetail(
            gateway.httpText(ep2.port, "/api/books/${SelfTestBook().bookKey}").body)!!
        val prog = EngineData.readProgress(
            gateway.httpText(ep2.port, "/api/books/${SelfTestBook().bookKey}/progress").body)
        assertEquals("重启后继续阅读落点", 2, detail.resumeIndex(prog))
        ev("重启后详情：继续阅读落点=${detail.resumeIndex(prog)}")

        // 5) 漫画：仍在书库，进度原样
        val manga = EngineData.manga(gateway.httpText(ep2.port, "/api/manga/library").body)
        val mc = manga.firstOrNull { it.comicId == comic.comicIdValue }
        assertTrue("重启后书库里应仍有自检漫画", mc != null)
        assertEquals("重启后漫画进度 pos",
            "${SelfTestComic.CH2_NAME} P2", mc!!.readPos)
        assertTrue("重启后应仍视为已开始阅读", mc.started)
        ev("重启后漫画：read_pos=${mc.readPos}")

        // 6) 队列可用，且中断任务被收敛（不能有幽灵 running）
        val tasksBody = gateway.httpText(ep2.port, "/api/tasks").body
        assertTrue("队列接口可用", tasksBody.contains("tasks"))
        val tasks = EngineData.tasks(tasksBody)
        assertTrue("重启后不应存在仍在运行的幽灵任务：${tasks.filter { it.running }.map { it.id }}",
            tasks.none { it.running })
        ev("重启后队列：${tasks.size} 条记录，运行中 ${tasks.count { it.running }} 条（应为 0）")

        // 7) 恢复入口可用：对不存在的任务恢复必须明确失败（不假装成功）
        val bogus = gateway.httpPost(ep2.port, "/api/tasks/不存在的任务/resume")
        assertFalse("恢复不存在的任务不应返回 2xx", bogus.ok)
        ev("恢复不存在的任务被拒：HTTP ${bogus.code}")
    }
}
