package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Test

class PendingChapterTaskTest {
    @Test fun pendingCountIsParsedWithoutChangingCurrentBatchProgress() {
        val task = EngineData.tasks("""{"tasks":[{"id":"manga_copy:comic","type":"manga",
            "status":"running","progress":{"done":1,"total":3,"pending_chapters":2}}]}""").single()
        assertEquals(2, task.pendingChapters)
        assertEquals(1, task.done)
        assertEquals(3, task.total)
    }

    @Test fun missingAndNegativePendingCountsRemainCompatible() {
        val old = EngineData.tasks("""{"tasks":[{"id":"old","type":"manga"}]}""").single()
        val bad = EngineData.tasks("""{"tasks":[{"id":"bad","type":"manga",
            "progress":{"pending_chapters":-2}}]}""").single()
        assertEquals(0, old.pendingChapters)
        assertEquals(0, bad.pendingChapters)
    }
}
