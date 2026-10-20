package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/** 在 Android runtime 上验证 Uri 编码参与后的在线/本地图片 URL 分流。 */
@RunWith(AndroidJUnit4::class)
class MangaPageUrlPolicyTest {
    @Test fun localCatalogNeverReturnsCdnAddress() {
        val url = resolveMangaPageUrl(
            8765, "jm", "42", "ch1",
            MangaPageEntry("https://cdn.example/image.jpg", local = false, lazy = false),
            index = 4, tick = 0, localCatalog = true,
        )
        assertEquals(
            "http://127.0.0.1:8765/api/manga/jm/42/chapter/ch1/img/4?catalog=local",
            url,
        )
        assertTrue("本地阅读图片必须走 loopback", url.startsWith("http://127.0.0.1:"))
    }

    @Test fun localApiEntryIsForcedToLocalAndKeepsRetryKey() {
        val url = resolveMangaPageUrl(
            8765, "jm", "42", "ch1",
            MangaPageEntry(
                "/api/manga/jm/42/chapter/ch1/img/2?catalog=online&x=1",
                local = true, lazy = false,
            ),
            index = 2, tick = 2, localCatalog = true,
        )
        assertEquals(
            "http://127.0.0.1:8765/api/manga/jm/42/chapter/ch1/img/2?x=1&catalog=local&r=2",
            url,
        )
    }

    @Test fun onlineCdnStaysDirectUntilRetryUsesServerProxy() {
        val entry = MangaPageEntry("https://cdn.example/image.jpg", false, false)
        assertEquals("https://cdn.example/image.jpg", resolveMangaPageUrl(
            8765, "jm", "42", "ch1", entry, 0, 0, localCatalog = false,
        ))
        val retry = resolveMangaPageUrl(
            8765, "jm", "42", "ch1", entry, 0, 1, localCatalog = false,
        )
        assertTrue(retry.startsWith("http://127.0.0.1:8765/api/manga/jm/42/chapter/ch1/proxy?u="))
    }
}
