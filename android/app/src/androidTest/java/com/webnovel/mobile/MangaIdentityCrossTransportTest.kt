package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.UUID

/** Cross-client identity regression against the real embedded Flask API and durable JSON stores. */
@RunWith(AndroidJUnit4::class)
class MangaIdentityCrossTransportTest {
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext
    private lateinit var gateway: EngineGateway
    private lateinit var endpoint: EngineEndpoint
    private val comicId = "identity-${UUID.randomUUID()}"
    private val aliases = listOf("copymanga_web", "copymanga")

    private fun store(name: String) = File(OfflineStore.runtimeDir(context), "manga/$name")

    private fun addRows(file: File, rows: Map<String, JSONObject>) {
        file.parentFile?.mkdirs()
        val root = if (file.isFile) JSONObject(file.readText(Charsets.UTF_8)) else JSONObject()
        rows.forEach { (key, value) -> root.put(key, value) }
        file.writeText(root.toString(), Charsets.UTF_8)
    }

    private fun removeFixtureRows(file: File) {
        if (!file.isFile) return
        runCatching {
            val root = JSONObject(file.readText(Charsets.UTF_8))
            aliases.forEach { root.remove("$it:$comicId") }
            if (root.length() == 0) file.delete()
            else file.writeText(root.toString(), Charsets.UTF_8)
        }
    }

    @Before fun setUp() = runBlocking {
        gateway = EngineGateway(context)
        val ready = gateway.connect() as? EngineState.Ready
            ?: throw AssertionError("本机引擎未就绪")
        endpoint = ready.endpoint
        val history = store("_history.json")
        val favorites = store("_favorites.json")
        addRows(history, mapOf(
            "copymanga_web:$comicId" to JSONObject()
                .put("title", "跨端身份测试").put("idx", 0).put("pos", "第1话 P1")
                .put("read_chapter_ids", org.json.JSONArray().put("ch-1"))
                .put("ts", 100),
            "copymanga:$comicId" to JSONObject()
                .put("title", "跨端身份测试").put("idx", 2).put("pos", "第3话 P1")
                .put("read_chapter_ids", org.json.JSONArray().put("ch-3"))
                .put("ts", 200),
        ))
        addRows(favorites, mapOf(
            "copymanga_web:$comicId" to JSONObject().put("title", "跨端身份测试").put("ts", 100),
            "copymanga:$comicId" to JSONObject().put("cover", "fixture-cover").put("ts", 200),
        ))
    }

    @After fun tearDown() {
        removeFixtureRows(store("_history.json"))
        removeFixtureRows(store("_favorites.json"))
    }

    @Test fun mergesLegacyAliasRowsButKeepsActualTransportForNavigation() = runBlocking {
        val historyResponse = gateway.httpText(endpoint.port, "/api/manga/history")
        assertTrue("history API failed: HTTP ${historyResponse.code}", historyResponse.ok)
        val history = EngineData.mangaHistory(historyResponse.body)
            .single { it.comicId == comicId }
        assertEquals("copymanga", history.identitySource)
        assertEquals("copymanga_web", history.source)
        assertEquals(setOf("ch-1", "ch-3"), history.readChapterIds)

        val favoriteResponse = gateway.httpText(endpoint.port, "/api/manga/favorites")
        assertTrue("favorites API failed: HTTP ${favoriteResponse.code}", favoriteResponse.ok)
        val favorite = EngineData.mangaFavorites(favoriteResponse.body)
            .single { it.comicId == comicId }
        assertEquals("copymanga", favorite.identitySource)
        assertEquals("copymanga_web", favorite.source)

        val write = gateway.httpPost(endpoint.port, "/api/manga/history", JSONObject()
            .put("source", "copymanga_web").put("comic_id", comicId)
            .put("idx", 1).put("pos", "第2话 P4").put("chapter_id", "ch-2")
            .put("chapter_label", "第2话").put("title", "跨端身份测试").toString())
        assertTrue("history write failed: HTTP ${write.code} ${write.body}", write.ok)

        val persisted = JSONObject(store("_history.json").readText(Charsets.UTF_8))
        val canonical = persisted.getJSONObject("copymanga:$comicId")
        assertEquals("copymanga_web", canonical.getString("transport_source"))
        assertTrue("legacy web key was not consolidated", !persisted.has("copymanga_web:$comicId"))
        val ids = canonical.getJSONArray("read_chapter_ids")
        assertEquals(setOf("ch-1", "ch-2", "ch-3"),
            (0 until ids.length()).map { ids.getString(it) }.toSet())
    }
}
