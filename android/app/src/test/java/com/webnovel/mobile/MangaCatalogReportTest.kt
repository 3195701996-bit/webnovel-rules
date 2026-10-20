package com.webnovel.mobile

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class MangaCatalogReportTest {
    private fun detail(rows: List<MangaChapter>) = MangaDetail(
        source="copymanga", comicId="fixture", identitySource="copymanga", title="核对作品",
        cover="https://image.invalid/?token=DO_NOT_EXPORT", author="PRIVATE_AUTHOR",
        intro="PRIVATE_DESCRIPTION", tags=emptyList(), chapters=rows,
        downloaded=setOf("one"), partialDownloaded=setOf("two"), sourceName="测试源",
        localOnly=true)

    @Test fun metadataPreservesDuplicateIdentitiesWithoutCopyingNetworkData() {
        val text = mangaCatalogReport(detail(listOf(
            MangaChapter("one","第41话","默认"), MangaChapter("two","第41话","默认"))))
        val report = JSONObject(text)
        assertEquals(2, report.getJSONArray("chapters").length())
        assertEquals(1, report.getJSONArray("same_name_entries").length())
        assertTrue(report.getJSONArray("chapters").getJSONObject(0).getBoolean("downloaded"))
        assertTrue(report.getJSONArray("chapters").getJSONObject(1).getBoolean("partial"))
        assertFalse(text.contains("DO_NOT_EXPORT"))
        assertFalse(text.contains("PRIVATE_AUTHOR"))
        assertFalse(text.contains("PRIVATE_DESCRIPTION"))
    }

    @Test fun longCatalogIsBoundedAndReportsOmission() {
        val report = JSONObject(mangaCatalogReport(detail((1..600).map {
            MangaChapter("id-$it","第${it}话","默认") })))
        assertEquals(600, report.getInt("chapter_count"))
        assertEquals(500, report.getJSONArray("chapters").length())
        assertEquals(100, report.getInt("omitted_chapters"))
    }
}
