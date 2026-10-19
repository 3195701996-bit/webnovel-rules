package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Test

class FavoriteFiltersTest {
    private fun favorite(id: String, title: String, unread: Int, source: String = "copymanga") =
        MangaFavorite(source, id, title, "", 0.0, unread, "")

    @Test fun unreadUsesActualSetBasedCountAndPreservesServerUpdateOrder() {
        val skipped = favorite("skipped", "社長與酒與星", 6)
        val caughtUp = favorite("caught-up", "已读作品", 0)
        val other = favorite("other", "其他作品", 1)
        val rows = listOf(skipped, caughtUp, other)
        assertEquals(listOf(skipped, other), filterMangaFavorites(rows, "", true))
        assertEquals(rows, filterMangaFavorites(rows, "  ", false))
        assertEquals(6, skipped.unreadCount)
    }

    @Test fun queryCombinesTermsAcrossTitleAndSourceWithoutChangingIdentities() {
        val row = favorite("stable-id", "My Comic", 2, "mangadex")
        assertEquals(listOf(row), filterMangaFavorites(listOf(row), "  MY  MangaDex ", true))
        assertEquals(emptyList<MangaFavorite>(), filterMangaFavorites(listOf(row), "unknown", false))
        assertEquals(emptyList<MangaFavorite>(), filterMangaFavorites(listOf(row.copy(unreadCount = -1)), "", true))
    }
}
