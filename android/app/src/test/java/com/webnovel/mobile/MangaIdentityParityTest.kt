package com.webnovel.mobile

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

class MangaIdentityParityTest {
    @Test
    fun sourceIdentityAndAliasesMatchSharedContract() {
        val fixture = requireNotNull(
            javaClass.getResourceAsStream("/manga_identity_parity.json"),
        ) { "共享漫画身份夹具缺失" }.bufferedReader().use { it.readText() }
        val cases = JSONObject(fixture).getJSONArray("cases")

        for (index in 0 until cases.length()) {
            val case = cases.getJSONObject(index)
            val transport = case.getString("transport")
            assertEquals(
                "${case.getString("name")}: canonical identity",
                case.getString("identity"),
                OfflineStore.canonicalMangaSource(transport),
            )
            val expectedAliases = case.getJSONArray("aliases").let { aliases ->
                (0 until aliases.length()).map(aliases::getString)
            }
            assertEquals(
                "${case.getString("name")}: accepted storage aliases",
                expectedAliases,
                OfflineStore.mangaSourceAliases(transport),
            )
        }
    }

    @Test
    fun favoriteUnreadBadgeMatchesSharedSkippedChapterFixture() {
        val fixture = requireNotNull(
            javaClass.getResourceAsStream("/manga_favorite_unread_parity.json"),
        ) { "共享漫画未读数夹具缺失" }.bufferedReader().use { it.readText() }
        val cases = JSONObject(fixture).getJSONArray("cases")

        for (index in 0 until cases.length()) {
            val case = cases.getJSONObject(index)
            val expected = case.getInt("expected_unread_count")
            val body = JSONObject().put("favorites", org.json.JSONArray().put(
                JSONObject()
                    .put("source", case.getString("source"))
                    .put("identity_source", case.getString("identity_source"))
                    .put("comic_id", case.getString("comic_id"))
                    .put("title", case.getString("name"))
                    .put("unread_count", expected),
            )).toString()
            val favorite = EngineData.mangaFavorites(body).single()

            assertEquals(case.getString("name"), expected, favorite.unreadCount)
            assertEquals(case.getString("name"), "copymanga", favorite.identitySource)
            assertEquals(case.getString("name"), "未读 $expected",
                mangaFavoriteUnreadBadgeLabel(favorite.unreadCount))
        }
    }
}
