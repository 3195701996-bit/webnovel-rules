package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DownloadTaskSectionsTest {
    @Test fun filtersAreDisplayOnlyAndPreserveIdentityOrderAndActivePrecedence() {
        val old = task("old").copy(title = "旧作品")
        val failed = task("failed", "error").copy(title = "第４２话", mangaKey = "copymanga:opaque-id")
        val active = task("active", "error", true).copy(title = "活动作品")
        val paused = task("paused", "paused")
        val tasks = listOf(old, failed, active, paused)
        assertEquals(listOf(failed), filterDownloadTasks(tasks, "42 COPYMANGA", "failed"))
        assertEquals(listOf(failed, paused), filterDownloadTasks(tasks, "", "recoverable"))
        assertEquals(listOf(active), filterDownloadTasks(tasks, "", "active"))
        assertEquals(listOf(old), filterDownloadTasks(tasks, "", "finished"))
        assertEquals(tasks, filterDownloadTasks(tasks, "", "all"))
        assertTrue(filterDownloadTasks(tasks, "opaque-id", "all").isEmpty())
        assertEquals("error", active.status)
        assertTrue(active.running)
        assertTrue(filterDownloadTasks(tasks, "42", "all").single() === failed)
    }

    @Test fun resumeFeedbackRequiresConfirmedStateTransition() {
        assertEquals("已请求继续：测试", mangaResumeMessage("测试", true, true, ""))
        assertTrue(mangaResumeMessage("测试", true, false, "no_task").contains("任务记录已不存在，未启动下载"))
        assertTrue(mangaResumeMessage("测试", true, false, "no_task").contains("重新选择章节"))
        assertTrue(mangaResumeMessage("测试", false, true, "").startsWith("未确认"))
        assertTrue(mangaResumeMessage("测试", true, false, "").startsWith("未确认"))
    }
    private fun task(id: String, status: String = "done", running: Boolean = false) =
        EngineData.TaskItem(id, "manga", id, status, running, 1, 1, "", 0.0, 0.0, "", id)

    @Test fun completedMangaAfterFortyFiveOtherRecordsRemainsReachable() {
        val tasks = (1..45).map { task("old-$it") } + task("new-chapter-42")
        val sections = downloadTaskSections(tasks)
        assertEquals(tasks, sections.finished)
        assertEquals("new-chapter-42", sections.finished.last().id)
        assertEquals(46, sections.finished.size)
    }

    @Test fun activeFlagTakesPrecedenceOverStalePausedStatusWithoutDuplicateCards() {
        val active = task("active", "paused", running = true)
        val queued = task("queued", "queued")
        val paused = listOf(task("paused", "paused"), task("stopped", "stopped"), task("error", "error"))
        val stopping = task("stopping", "cancel")
        val finished = listOf(task("done"))
        val sections = downloadTaskSections(listOf(active, queued, stopping) + paused + finished)
        assertEquals(listOf(active, queued, stopping), sections.running)
        assertEquals(paused, sections.paused)
        assertEquals(finished, sections.finished)
        assertEquals(7, (sections.running + sections.paused + sections.finished).map { it.id }.distinct().size)
    }

    @Test fun stoppingKeepsPollingUntilWorkerPublishesStopped() {
        val stopping = task("stopping", "cancel")
        assertEquals("停止中 1/1", stopping.label)
        assertTrue(stopping.needsDownloadRefresh())
        val stopped = stopping.copy(status = "stopped")
        assertFalse(stopped.needsDownloadRefresh())
        assertEquals(listOf(stopped), downloadTaskSections(listOf(stopped)).paused)
        assertTrue(downloadTaskSections(listOf(stopped)).finished.isEmpty())
    }
}
