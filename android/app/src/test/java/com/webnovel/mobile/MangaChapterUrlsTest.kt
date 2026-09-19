package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 章节图片批量接口（/chapter/<id>/urls）解析：每页加载方式（本地直出/懒下载通道/
 * 源站 CDN 直连）必须原样保留——阅读器按它决定走引擎还是直连（对齐网页端）。
 */
class MangaChapterUrlsTest {

    @Test fun parsesAllThreeLoadingModes() {
        val body = """
          {"count": 3, "source": "s", "comic_id": "c", "chapter_id": "ch1",
           "images": [
             {"local": true,  "url": "/api/manga/s/c/chapter/ch1/img/0"},
             {"local": false, "lazy": true, "url": "/api/manga/s/c/chapter/ch1/img/1"},
             {"local": false, "url": "https://cdn.example.com/p2.jpg"}
           ]}
        """.trimIndent()
        val p = EngineData.mangaChapterUrls(body)!!
        assertEquals(3, p.count)
        assertEquals(3, p.entries.size)
        // 本地直出
        assertTrue(p.entries[0].local)
        assertTrue(p.entries[0].url.startsWith("/api/"))
        // 服务器懒下载通道（jm 等混淆源）
        assertTrue(p.entries[1].lazy)
        assertTrue(p.entries[1].url.startsWith("/api/"))
        // 源站 CDN 直连：URL 必须原样保留（阅读器据此绕过引擎中转）
        assertEquals("https://cdn.example.com/p2.jpg", p.entries[2].url)
        assertEquals(false, p.entries[2].local)
        assertEquals(false, p.entries[2].lazy)
        assertEquals("ch1", p.chapterId)
    }

    @Test fun staleProcessingCarriedThrough() {
        val body = """
          {"count": 1, "chapter_id": "ch", "local_only": true,
           "stale_processing": true, "stale_reason": "本章图片是旧版处理缓存，需联网重新获取",
           "images": [{"local": true, "url": "/api/manga/s/c/chapter/ch/img/0"}]}
        """.trimIndent()
        val p = EngineData.mangaChapterUrls(body)!!
        assertTrue(p.staleProcessing)
        assertEquals("本章图片是旧版处理缓存，需联网重新获取", p.staleReason)
        assertTrue(p.local)
    }

    @Test fun emptyImagesRejected() {
        assertNull(EngineData.mangaChapterUrls("""{"count": 0, "images": []}"""))
    }

    @Test fun garbageRejected() {
        assertNull(EngineData.mangaChapterUrls("not json"))
        assertNull(EngineData.mangaChapterUrls("""{"images": []}"""))
    }
}
