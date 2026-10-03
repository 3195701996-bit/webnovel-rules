package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Test

class MangaChapterStatusTest {
    private fun label(
        inTask: Boolean = false,
        running: Boolean = false,
        current: Boolean = false,
        downloaded: Boolean = false,
        partial: Boolean = false,
        reading: Boolean = false,
    ) = mangaChapterStatusLabel(reading, inTask, running, current, downloaded, partial)

    @Test fun distinguishesQueuedDownloadAndTopUpDuringLiveTask() {
        assertEquals("排队中", label(inTask = true))
        assertEquals("排队中", label(inTask = true, running = true))
        assertEquals("下载中", label(inTask = true, running = true, current = true))
        assertEquals("补齐中",
            label(inTask = true, running = true, current = true, partial = true))
    }

    @Test fun stableStatesRemainHonestAndReadingTakesPriority() {
        assertEquals("已下载", label(downloaded = true))
        assertEquals("部分下载 · 可补齐", label(partial = true))
        assertEquals("未下载", label())
        assertEquals("在读",
            label(inTask = true, running = true, current = true, reading = true))
    }

    @Test fun downloadStatusCarriesTheCurrentStableChapterIdentity() {
        val status = EngineData.mangaDlStatus(
            """{"status":"running","current_chapter_id":"ch-current","chapters":[{"id":"ch-current"},{"id":"ch-next"}]}""",
        )
        assertEquals(setOf("ch-current", "ch-next"), status.chapterIds)
        assertEquals("ch-current", status.currentChapterId)
    }
}
