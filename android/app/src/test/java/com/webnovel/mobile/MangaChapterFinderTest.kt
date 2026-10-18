package com.webnovel.mobile

import org.junit.Assert.*
import org.junit.Test

class MangaChapterFinderTest {
    @Test fun sameSuffixChapterIdsHaveDistinctMinimalIdentityHints() {
        val a = "3ed54ac2-c519-11f1-a73b-fa163e02432f"
        val b = "3b77390f-c4d7-11f1-8ff4-fa163e02432f"
        assertEquals(a.takeLast(12), b.takeLast(12))
        val hints = mangaChapterIdentityHints(listOf(
            MangaChapter(a, "第41話", "默认"), MangaChapter(b, " 第41話 ", "默认")))
        assertEquals("3ed54ac2…", hints[a])
        assertNotEquals(hints[a], hints[b])
        val prefix = "same-prefix-".repeat(5)
        val long = mangaChapterIdentityHints(listOf(
            MangaChapter(prefix + "a", "同名", ""), MangaChapter(prefix + "b", "同名", "")))
        assertNotEquals(long[prefix + "a"], long[prefix + "b"])
        assertTrue(mangaChapterIdentityHints(listOf(
            MangaChapter("same", "one", ""), MangaChapter("same", "one", ""))).isEmpty())
    }

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
