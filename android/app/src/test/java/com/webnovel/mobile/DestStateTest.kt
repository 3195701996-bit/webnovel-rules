package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class DestStateTest {
    @Test
    fun everyNavigationDestinationSurvivesSaveAndRestore() {
        val destinations = listOf(
            Dest.Detail("novel/key with spaces"),
            Dest.Novel("book-1", index = 7, pct = 63),
            Dest.MangaDetail("copymanga_web", "comic/1", localCatalog = true),
            Dest.MangaDetail("copymanga", "comic/1", localCatalog = false),
            Dest.Manga("copymanga_web", "comic/1", index = 9, page = 23,
                trusted = false, note = "目录已更新", startChapterId = "卷/第1卷",
                startLabel = "第1卷", localCatalog = true),
            Dest.Explore,
            Dest.MangaSources,
            Dest.MangaSource("source/1", "来源 名称"),
            Dest.ReaderPrefs,
            Dest.History,
            Dest.NovelSearch,
            Dest.MangaSearch("copymanga", "latest", "100% 漫画"),
            Dest.OfflineNovel("offline-book", 4),
            Dest.OfflineManga("copymanga", "offline/comic"),
            Dest.Sources,
            Dest.Storage,
            Dest.Backup,
            Dest.Web("/diagnostics/path?q=100%25", "诊断页"),
        )

        destinations.forEach { destination ->
            assertEquals("destination failed JSON round-trip: $destination",
                destination, decodeDest(encodeDest(destination)))
        }
    }

    @Test
    fun localAndOnlineCatalogDestinationsRemainDistinctAfterRestore() {
        val localDetail = Dest.MangaDetail("mangadex", "comic-1", localCatalog = true)
        val onlineDetail = Dest.MangaDetail("mangadex", "comic-1", localCatalog = false)
        val localReader = Dest.Manga("mangadex", "comic-1", index = 2, page = 5,
            localCatalog = true)
        val onlineReader = localReader.copy(localCatalog = false)
        val explicitlySelectedChapter = localReader.copy(
            index = 1, startChapterId = "chapter-2", startLabel = "第2话")

        assertNotEquals(destKey(localDetail), destKey(onlineDetail))
        assertNotEquals(destKey(localReader), destKey(onlineReader))
        assertNotEquals("a user-selected chapter must not restore the prior reader session",
            destKey(localReader), destKey(explicitlySelectedChapter))
        assertEquals(localDetail, decodeDest(encodeDest(localDetail)))
        assertEquals(onlineDetail, decodeDest(encodeDest(onlineDetail)))
        assertEquals(localReader, decodeDest(encodeDest(localReader)))
        assertEquals(onlineReader, decodeDest(encodeDest(onlineReader)))
    }

    @Test
    fun reopeningTheSameChapterGetsFreshReaderStateButRestorationKeepsSession() {
        val firstLaunch = Dest.Manga("copymanga_web", "comic-1", index = 0, page = 0,
            startChapterId = "chapter-1", startLabel = "第1话", localCatalog = true)
        val reopened = Dest.Manga("copymanga_web", "comic-1", index = 0, page = 0,
            startChapterId = "chapter-1", startLabel = "第1话", localCatalog = true)

        assertNotEquals("each explicit tap must not reuse stale reader state",
            destKey(firstLaunch), destKey(reopened))
        assertEquals("restoring the same navigation stack must retain its reader state",
            destKey(firstLaunch), destKey(decodeDest(encodeDest(firstLaunch))!!))
        assertEquals(firstLaunch, decodeDest(encodeDest(firstLaunch)))
    }

    @Test
    fun legacyReaderStackWithoutSessionIdRestoresSafely() {
        val restored = decodeDest("""{"type":"manga","source":"source","comic":"comic", "index":0,"page":0,"chapter_id":"chapter-1"}""")
        assertNotNull(restored)
        assertTrue(restored is Dest.Manga)
        assertTrue((restored as Dest.Manga).sessionId.isNotBlank())
    }

    @Test
    fun unknownOrMalformedSavedDestinationIsDroppedSafely() {
        assertNull(decodeDest("not json"))
        assertNull(decodeDest("{\"type\":\"future_destination\"}"))
        assertNull(decodeDest("{\"type\":\"manga\",\"source\":\"x\"}"))
    }
}
