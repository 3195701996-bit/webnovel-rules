package com.webnovel.mobile

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

class NovelDownloadBadgeTest {
    @Test
    fun downloadCountsAreNotPublicationOrRunningStatus() {
        val rows = JSONArray(javaClass.getResource("/novel_download_status.json")!!.readText())
        for (index in 0 until rows.length()) {
            val row = rows.getJSONObject(index)
            val book = EngineData.novels(JSONObject().put("books", JSONArray().put(row)).toString()).single()
            assertEquals("case $index", row.getBoolean("complete"), book.downloadComplete)
            assertEquals("case $index", row.getString("label"), book.downloadBadge)
        }
    }
}
