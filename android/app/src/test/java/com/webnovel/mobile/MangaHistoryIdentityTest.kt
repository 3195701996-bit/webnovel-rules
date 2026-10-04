package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class MangaHistoryIdentityTest {
    @Test fun readerControlHotZoneIsSmallAndCentered() {
        val width = 1000f
        val height = 2000f
        assertEquals(true, isReaderControlHotZone(500f, 1000f, width, height))
        assertEquals(true, isReaderControlHotZone(390f, 860f, width, height))
        assertEquals(true, isReaderControlHotZone(610f, 1140f, width, height))
        assertEquals(false, isReaderControlHotZone(389f, 1000f, width, height))
        assertEquals(false, isReaderControlHotZone(611f, 1000f, width, height))
        assertEquals(false, isReaderControlHotZone(500f, 859f, width, height))
        assertEquals(false, isReaderControlHotZone(500f, 1141f, width, height))
        assertEquals(false, isReaderControlHotZone(250f, 1000f, width, height))
    }

    @Test fun readerUrlCacheNeverCrossesLocalAndOnlineCatalogModes() {
        val online = MangaChapterPages(count = 2, local = false, images = listOf("a", "b"))
        val local = MangaChapterPages(count = 1, local = true, images = listOf("a"))

        assertEquals(true, readerCacheMatchesCatalog(online, localCatalog = false))
        assertEquals(false, readerCacheMatchesCatalog(online, localCatalog = true))
        assertEquals(false, readerCacheMatchesCatalog(local, localCatalog = false))
        assertEquals(true, readerCacheMatchesCatalog(local, localCatalog = true))
    }

    @Test fun parsesTheWholeResolvedReadSetAlongsideTheCurrentPosition() {
        val history = EngineData.mangaHistory(
            """{"history":[{"source":"copy","comic_id":"series","idx":4,"pos":"第5话 P8","title":"作品","ts":7,"read_chapter_ids":["ch1","ch3","ch5"]}]}""",
        )
        assertEquals(1, history.size)
        assertEquals(setOf("ch1", "ch3", "ch5"), history.single().readChapterIds)
    }

    @Test fun oldHistoryWithoutReadSetRemainsCompatible() {
        val history = EngineData.mangaHistory(
            """{"history":[{"source":"copy","comic_id":"series","idx":0,"pos":"第1话 P1"}]}""",
        )
        assertEquals(emptySet<String>(), history.single().readChapterIds)
    }

    @Test fun copyMangaAppAndWebEndpointsResolveToSameDurableIdentity() {
        assertEquals(true, sameMangaIdentitySource("copymanga", "copymanga_web"))
        assertEquals(false, sameMangaIdentitySource("jm", "copymanga_web"))
        val history = EngineData.mangaHistory(
            """{"history":[{"source":"copymanga","identity_source":"copymanga","source_aliases":["copymanga","copymanga_web"],"comic_id":"series","idx":2,"pos":"第3话 P1"}]}""",
        ).single()
        assertEquals("copymanga", history.identitySource)
    }

    @Test fun favoriteParserKeepsCanonicalIdentityButTransportSourceForRouting() {
        val favorite = EngineData.mangaFavorites(
            """{"favorites":[{"source":"copymanga","identity_source":"copymanga","comic_id":"series","title":"作品","unread_count":6}]}""",
        ).single()
        assertEquals("copymanga", favorite.source)
        assertEquals("copymanga", favorite.identitySource)
        assertEquals(6, favorite.unreadCount)
        assertEquals("未读 6", mangaFavoriteUnreadBadgeLabel(favorite.unreadCount))
        assertNull(mangaFavoriteUnreadBadgeLabel(0))
        assertNull(mangaFavoriteUnreadBadgeLabel(-1))
    }

    @Test fun malformedFavoriteEnvelopeIsNotConfusedWithAValidEmptyShelf() {
        assertNull(EngineData.mangaFavoritesOrNull("not-json"))
        assertNull(EngineData.mangaFavoritesOrNull("""{"ok":true}"""))
        assertNull(EngineData.mangaFavoritesOrNull("""{"favorites":[null]}"""))
        assertNull(EngineData.mangaFavoritesOrNull("""{"favorites":[{}]}"""))
        assertNull(EngineData.mangaFavoritesOrNull(
            """{"favorites":[],"recoverable":true}""",
        ))
        assertEquals(emptyList<MangaFavorite>(),
            EngineData.mangaFavoritesOrNull("""{"favorites":[]}"""))
    }

    @Test fun readCacheSeparatesLocalOnlyAndOnlineMixedCatalogs() {
        val online = EngineData.mangaDetail(
            """{"source":"copy","comic_id":"series","title":"作品","chapters":[{"id":"old","name":"第1话"},{"id":"new","name":"第2话"}],"downloaded":["old"]}""",
        )!!
        val local = online.copy(chapters = listOf(online.chapters.first()),
            downloaded = setOf("old"), localOnly = true)
        MangaReadCache.putDetail("copy", "series", online, localCatalog = false)
        MangaReadCache.putDetail("copy", "series", local, localCatalog = true)

        assertEquals(listOf("old", "new"), MangaReadCache.getDetail(
            "copy", "series", localCatalog = false)?.readingChapters?.map { it.id })
        assertEquals(listOf("old"), MangaReadCache.getDetail(
            "copy", "series", localCatalog = true)?.readingChapters?.map { it.id })

        MangaReadCache.invalidate("copy", "series")
        assertEquals(null, MangaReadCache.getDetail("copy", "series", false))
        assertEquals(null, MangaReadCache.getDetail("copy", "series", true))
    }
}
