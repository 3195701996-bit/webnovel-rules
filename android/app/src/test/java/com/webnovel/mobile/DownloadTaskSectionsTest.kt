package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Test

class DownloadTaskSectionsTest {
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
        val finished = listOf(task("done"), task("cancelled", "cancel"))
        val sections = downloadTaskSections(listOf(active, queued) + paused + finished)
        assertEquals(listOf(active, queued), sections.running)
        assertEquals(paused, sections.paused)
        assertEquals(finished, sections.finished)
        assertEquals(7, (sections.running + sections.paused + sections.finished).map { it.id }.distinct().size)
    }
}
