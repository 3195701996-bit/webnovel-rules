package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class MangaUpdatePreviewTest {
    @Test
    fun previewPreservesChapterIdentityAndNameSeparately() {
        val result = EngineData.mangaUpdateCheck("""{
            "ok":true,"has_update":true,"missing_count":2,"latest":"第42话",
            "missing":[{"id":"special","name":"番外"},{"id":"new-42","name":"第42话"}]
        }""")
        assertEquals(listOf("special", "new-42"), result.missingIds)
        assertEquals(listOf("番外", "第42话"), result.missingNames)
        assertTrue(result.label.contains("2 个章节未下载"))
        assertFalse(result.label.contains("2 个新话"))
    }

    @Test
    fun oldIdOnlyResponseRemainsDownloadableWithoutInventedNames() {
        val result = EngineData.mangaUpdateCheck("""{
            "ok":true,"missing":["chapter-id"]
        }""")
        assertEquals(listOf("chapter-id"), result.missingIds)
        assertTrue(result.missingNames.isEmpty())
    }

    @Test
    fun checkFailureIsNotPresentedAsUpToDate() {
        val result = EngineData.mangaUpdateCheck("""{"ok":false,"error":"目录读取失败"}""")
        assertTrue(result.label.contains("检查失败"))
        assertFalse(result.label.contains("已是最新"))
    }
}
