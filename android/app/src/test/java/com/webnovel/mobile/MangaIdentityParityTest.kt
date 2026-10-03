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
}
