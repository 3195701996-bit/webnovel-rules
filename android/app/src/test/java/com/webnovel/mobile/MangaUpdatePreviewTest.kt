package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class MangaUpdatePreviewTest {
    @Test
    fun librarySummaryDoesNotCallOldMissingChaptersNewOrFailuresCurrent() {
        val missing = EngineData.mangaUpdateCheck("""{"ok":true,"has_update":false,"missing_count":1,"missing":["old"]}""")
        val failed = EngineData.mangaUpdateCheck("""{"ok":false,"error":"离线"}""")
        val summary = MangaLibraryUpdate(false, 2, 2, listOf("旧缺章" to missing, "失败" to failed))
        assertEquals(1, summary.withUpdate.size)
        assertTrue(summary.label.contains("1 部有待下载内容"))
        assertTrue(summary.label.contains("1 部检查失败"))
        assertFalse(summary.label.contains("有新话"))
        assertFalse(summary.label.contains("全部已是最新"))
        assertTrue(MangaLibraryUpdate(false, 1, 1, listOf("失败" to failed)).label.contains("检查失败"))
    }

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

    @Test
    fun catalogChangesWarnWithoutClaimingEveryMissingItemIsNew() {
        val result = EngineData.mangaUpdateCheck("""{
            "ok":true,"missing_count":3,"missing":[],"catalog_diff":{
                "baseline_available":true,"added_missing_count":1,"existing_missing_count":2,
                "renamed_count":1,"same_name_new_id_count":1,"duplicate_name_count":1
            }
        }""")
        assertEquals("目录新增 1 项，原目录待补 2 项", result.catalogSummary)
        assertTrue(result.catalogWarning.contains("同名新身份"))
        assertTrue(result.catalogWarning.contains("不会自动合并或删除"))
    }
}
