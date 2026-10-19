package com.webnovel.mobile

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class LocalMediaInventoryTest {
    private fun payload(rows: JSONArray = JSONArray()) = JSONObject()
        .put("ok", true).put("local_only", true).put("source", "copymanga")
        .put("comic_id", "literal%2Fid").put("count", rows.length()).put("media", rows)
    private fun row(id: String, partial: Boolean) = JSONObject().put("id", id)
        .put("name", "同名").put("downloaded", !partial).put("partial", partial)

    @Test fun exactIdsAndPartialStateArePreserved() {
        val parsed = EngineData.mangaLocalMedia(payload(JSONArray().put(row("old", true))
            .put(row("new", false))).toString())!!
        assertEquals("literal%2Fid", parsed.comicId)
        assertEquals(listOf("old", "new"), parsed.media.map { it.id })
        assertTrue(parsed.media[0].partial)
        assertFalse(parsed.media[1].partial)
        assertTrue(EngineData.mangaLocalMedia(payload().toString())!!.media.isEmpty())
    }
    @Test fun malformedSelectionsAreRejectedRatherThanSilentlyDropped() {
        val duplicate = payload(JSONArray().put(row("one", true)).put(row("one", false)))
        assertNull(EngineData.mangaLocalMedia(duplicate.toString()))
        for (bad in listOf(
            payload().put("ok", false), payload().put("local_only", false),
            payload().put("count", 1), payload().put("comic_id", JSONObject.NULL),
            payload(JSONArray().put(row("one", true).put("downloaded", true))),
            payload(JSONArray().put(row("one", true).put("id", 1))),
        )) assertNull(bad.toString(), EngineData.mangaLocalMedia(bad.toString()))
    }
}
