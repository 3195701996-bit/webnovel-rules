package com.webnovel.mobile

import org.junit.Assert.*
import org.junit.Test

class NovelChapterFiltersTest {
    private val chapters = listOf(
        ChapterItem(1, "第一章 魔法 世界", true, false, ""),
        ChapterItem(12, "卷二 第一章 魔法", false, true, "失败说明"),
        ChapterItem(112, "第112章 世界", true, false, ""),
    )
    @Test fun numericQueriesUseExactOriginalDirectoryIndex() {
        assertSame(chapters[1], filterNovelChapters(chapters, "００１２", "all").single())
        assertTrue(filterNovelChapters(chapters, "0", "all").isEmpty())
        assertTrue(filterNovelChapters(chapters, "99999999999999999999999999", "all").isEmpty())
    }
    @Test fun wordsCombineWithoutMutatingOrderOrIdentity() {
        assertSame(chapters[0], filterNovelChapters(chapters, "魔法　世界", "all").single())
        val matches = filterNovelChapters(chapters, "第一章", "all")
        assertEquals(2, matches.size)
        assertSame(chapters[0], matches[0]); assertSame(chapters[1], matches[1])
    }
    @Test fun filtersRespectActualDownloadStateNotFailureOrReadingPosition() {
        assertSame(chapters[1], filterNovelChapters(chapters, "", "missing").single())
        assertEquals(listOf(chapters[0], chapters[2]), filterNovelChapters(chapters, "", "downloaded"))
        assertTrue(filterNovelChapters(chapters, "12", "downloaded").isEmpty())
        assertSame("初次无筛选时不复制或重新扫描长目录", chapters, filterNovelChapters(chapters, "  ", "all"))
    }
}
