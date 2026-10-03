package com.webnovel.mobile

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 「继续阅读」落点 + 下载任务文案（纯逻辑，JVM 运行）。
 *
 * 两处都是用户会直接撞上的判断：
 *   1. 续读落点：服务端 `/progress` 返回的是**原样存储**的位置、不做范围校验，
 *      而目录会变（源站删章、或换源后目录更短）。旧实现直接用它 →
 *      阅读器显示「加载失败：HTTP 404 · 章节序号越界」；漫画侧 2026-09-17
 *      就修过同类问题（`in chapters.indices`），小说侧此前没有对齐。
 *   2. 任务文案：下载页显示的"已完成 N / 为什么停下"就是这里拼的，
 *      史上踩过 stop_kind/checkpoint 缺失与"已完成"误标。
 */
class ResumeAndTaskLabelsTest {

    private fun ev(line: String) = println("RESUME_TASK_EVIDENCE $line")

    private fun detail(count: Int, downloaded: Set<Int> = emptySet()): BookDetail {
        val arr = JSONArray()
        for (i in 1..count) {
            arr.put(JSONObject().put("index", i).put("name", "第${i}章")
                .put("downloaded", downloaded.contains(i)).put("failed", false))
        }
        val body = JSONObject().put("key", "k").put("name", "书")
            .put("total", count).put("done", downloaded.size).put("chapters", arr)
        return EngineData.bookDetail(body.toString())!!
    }

    private fun progress(idx: Int, pct: Int = 0) =
        ReadProgress(idx = idx, pct = pct, name = "第${idx}章")

    @Test
    fun progressInsideRangeIsUsed() {
        assertEquals(7, detail(20).resumeIndex(progress(7)))
    }

    @Test
    fun progressBeyondRangeIsClampedNotPropagated() {
        // 目录变短（20 章），进度还停在 999 章 → 必须收敛到最后一章，
        // 否则阅读器会请求 /chapter/999 → HTTP 404「章节序号越界」
        val d = detail(20)
        assertEquals(20, d.resumeIndex(progress(999)))
        ev("越界进度 999 / 目录 20 章 → 落点 ${d.resumeIndex(progress(999))}")
    }

    @Test
    fun missingProgressFallsBackToFirstDownloadedChapter() {
        val d = detail(20, downloaded = setOf(5, 6, 7))
        assertEquals(5, d.resumeIndex(null))
        assertEquals(5, d.resumeIndex(progress(0)))     // idx=0 表示未读（started=false）
    }

    @Test
    fun nothingDownloadedFallsBackToFirstChapter() {
        assertEquals(1, detail(20).resumeIndex(null))
    }

    @Test
    fun emptyCatalogNeverReturnsZeroOrNegative() {
        // 目录还没抓到（刚加入书架）时不能让阅读器去请求第 0 章
        val d = detail(0)
        assertEquals(1, d.resumeIndex(null))
        assertEquals(1, d.resumeIndex(progress(3)))
    }

    @Test
    fun sparseIndicesAreAlsoRespected() {
        // 防御：若服务端将来给出非连续下标，落点仍必须在范围内
        val arr = JSONArray()
        for (i in intArrayOf(3, 4, 9)) {
            arr.put(JSONObject().put("index", i).put("name", "第${i}章").put("downloaded", false))
        }
        val d = EngineData.bookDetail(
            JSONObject().put("key", "k").put("total", 3).put("chapters", arr).toString())!!
        assertEquals(9, d.resumeIndex(progress(100)))
        assertEquals(3, d.resumeIndex(progress(1)))
    }

    // ── 下载任务文案 ────────────────────────────────────────────────
    private fun tasks(json: String) = EngineData.tasks(json)

    @Test
    fun mangaTaskProgressAndStopReason() {
        val body = JSONObject().put("tasks", JSONArray().put(
            JSONObject().put("id", "manga_copymanga:jurenmeiman").put("type", "manga")
                .put("title", "巨人").put("status", "stopped").put("running", false)
                .put("progress", JSONObject().put("done", 4).put("total", 12)
                    .put("images_done", 96).put("images_total", 288))
                .put("stop_kind", "process_restart")
                .put("stop_reason", "应用进程或前台服务已停止，下载随之中断；可点「继续」接着下。")
                .put("resumable", true)
                .put("checkpoint", JSONObject().put("done", 4).put("total", 12).put("failed", 0))
        )).toString()
        val t = tasks(body).single()
        assertEquals(4, t.done)
        assertEquals(12, t.total)
        assertEquals(4, t.checkpointDone)
        assertEquals(12, t.checkpointTotal)
        assertTrue("停了就要显示原因：${t.stopReason}", t.showStopReason)
        assertTrue(t.resumableFlag)
        ev("漫画任务：${t.label} / ${t.stopReason}")
    }

    @Test
    fun recoveredQueueIsShownAsResumableStoppedTask() {
        val body = JSONObject().put("tasks", JSONArray()
            .put(JSONObject().put("id", "manga_source:queued").put("type", "manga")
                .put("title", "排队样本").put("status", "queued").put("running", false)
                .put("resumable", false))
            .put(JSONObject().put("id", "manga_source:recovered").put("type", "manga")
                .put("title", "中断样本").put("status", "stopped").put("running", false)
                .put("resumable", true).put("stop_kind", "process_restart")
                .put("stop_reason", "任务仍在等待队列，可点继续开始下载")))
            .toString()

        val parsed = tasks(body)
        val orphanQueue = parsed.first { it.id.endsWith(":queued") }
        val recovered = parsed.first { it.id.endsWith(":recovered") }
        assertFalse("仅有 queued 状态不能代表重启后仍有活队列", orphanQueue.resumable)
        assertTrue("服务端将孤立队列收敛为 stopped 后，界面必须显示继续入口",
            recovered.resumable)
        assertTrue(recovered.showStopReason)
        assertTrue(recovered.stopReason.contains("等待队列"))
    }

    @Test
    fun finishedTaskDoesNotShowStopReason() {
        val body = JSONObject().put("tasks", JSONArray().put(
            JSONObject().put("id", "manga_a:b").put("type", "manga").put("title", "书")
                .put("status", "done").put("running", false)
                .put("progress", JSONObject().put("done", 5).put("total", 5))
                .put("stop_reason", "旧原因不该再显示")
        )).toString()
        val t = tasks(body).single()
        assertFalse("已完成的任务不得再显示停止原因", t.showStopReason)
        assertTrue("已完成的任务应显示完成数", t.label.contains("5"))
        ev("已完成：${t.label}")
    }

    @Test
    fun novelTaskWithoutCheckpointStillParses() {
        val body = JSONObject().put("tasks", JSONArray().put(
            JSONObject().put("id", "t1").put("type", "novel").put("title", "剑来")
                .put("status", "running").put("running", true)
                .put("progress", JSONObject().put("done", 111).put("total", 814)
                    .put("current", "第112章"))
        )).toString()
        val t = tasks(body).single()
        assertEquals(111, t.done)
        assertEquals(0, t.checkpointTotal)          // 缺字段按 0，不崩
        assertFalse(t.showStopReason)               // 正在跑不显示"为什么停"
        ev("小说任务：${t.label} 当前=${t.current}")
    }

    // ── 进度字段的两种形状（网页端 tasks.html 同款判据）──────────────────
    @Test
    fun novelTaskProgressUsesCompletedKey() {
        // 实测：小说任务的 progress 里只有 completed（爬虫写的那份），没有 done。
        // 客户端只读 done 时 → 进度永远 0/总数、进度条卡在 0。
        val body = JSONObject().put("tasks", JSONArray().put(
            JSONObject().put("id", "t1").put("type", "novel").put("title", "剑来")
                .put("status", "running").put("running", true)
                .put("progress", JSONObject().put("completed", 111).put("total", 1306)
                    .put("current", "第112章")))).toString()
        val t = tasks(body).single()
        assertEquals("小说任务必须从 completed 读进度", 111, t.done)
        assertEquals(1306, t.total)
        assertEquals(8, t.percent)          // 111/1306 ≈ 8.5% → 8
        assertTrue("标签要显示真实进度：${t.label}", t.label.contains("111/1306"))
        ev("小说任务进度：${t.label} percent=${t.percent}")
    }

    @Test
    fun mangaTaskProgressStillUsesDoneKey() {
        val body = JSONObject().put("tasks", JSONArray().put(
            JSONObject().put("id", "manga_a:b").put("type", "manga").put("title", "巨人")
                .put("status", "running").put("running", true)
                .put("progress", JSONObject().put("done", 4).put("total", 12)))).toString()
        val t = tasks(body).single()
        assertEquals(4, t.done)
        assertEquals(33, t.percent)
    }

    @Test
    fun runningTasksShowSourceProgressUnitsAndUnknownEstimate() {
        val known = tasks(JSONObject().put("tasks", JSONArray().put(
            JSONObject().put("id", "manga_a:b").put("type", "manga")
                .put("status", "running").put("running", true)
                .put("progress", JSONObject().put("done", 1).put("total", 4)
                    .put("speed", 2.5).put("eta", 90))
        )).toString()).single()
        assertTrue(known.speedLabel.contains("2.5 图/秒"))
        assertTrue(known.speedLabel.contains("约 90 秒"))

        val unknown = tasks(JSONObject().put("tasks", JSONArray().put(
            JSONObject().put("id", "manga_a:c").put("type", "manga")
                .put("status", "running").put("running", true)
                .put("progress", JSONObject().put("done", 0).put("total", 1))
        )).toString()).single()
        assertTrue(unknown.speedLabel.contains("速度估算中"))

        val novel = tasks(JSONObject().put("tasks", JSONArray().put(
            JSONObject().put("id", "novel-a").put("type", "novel")
                .put("status", "running").put("running", true)
                .put("progress", JSONObject().put("completed", 1).put("total", 10)
                    .put("speed", 1.0).put("eta", JSONObject.NULL))
        )).toString()).single()
        assertTrue(novel.speedLabel.contains("1.0 章/秒"))
        ev("运行中任务展示按媒体类型区分的速度，未知速率明确显示估算中")
    }

    @Test
    fun missingProgressFieldsDoNotCrash() {
        val body = JSONObject().put("tasks", JSONArray().put(
            JSONObject().put("id", "t2").put("type", "novel").put("status", "queued")
                .put("running", false))).toString()
        val t = tasks(body).single()
        assertEquals(0, t.done)
        assertEquals(0, t.percent)
    }
}
