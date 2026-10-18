package com.webnovel.mobile

import org.junit.Assert.*
import org.junit.Test

class MangaChapterFinderTest {
    @Test fun filteredResultRetainsOriginalIdentityAndVolumeFirstIndex() {
        val rows = listOf(MangaChapter("volume","第01卷","单行本"),
            MangaChapter("old","第41話","默认"), MangaChapter("new","第42話","默认"))
        val result = findMangaChapters(rows, "42话").single()
        assertEquals(2, result.index)
        assertEquals("new", result.value.id)
        assertEquals("volume", findMangaChapters(rows, "单行本").single().value.id)
    }

    @Test fun sameNameEntriesRemainSelectableAndLargeCatalogIsBounded() {
        val rows = (1..5000).map { MangaChapter("id-$it", "同名", "默认") }
        val found = findMangaChapters(rows, "同名")
        assertEquals(100, found.size)
        assertEquals(100, found.map { it.value.id }.toSet().size)
        assertTrue(findMangaChapters(rows, "不存在").isEmpty())
    }
}
