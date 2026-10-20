package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class MangaFavoriteMutationTest {
    @Test
    fun addRequiresExplicitBusinessSuccess() {
        val failed = mangaFavoriteMutationResult(
            HttpText(code = 200, body = """{"ok":false,"error":"收藏文件只读"}"""),
            adding = true,
            wasFavorited = false,
        )
        assertFalse(failed.success)
        assertFalse(failed.favorited)
        assertEquals("收藏操作失败：收藏文件只读", failed.message)

        val added = mangaFavoriteMutationResult(
            HttpText(code = 200, body = """{"ok":true}"""),
            adding = true,
            wasFavorited = false,
        )
        assertTrue(added.success)
        assertTrue(added.favorited)
    }

    @Test
    fun deleteFailurePreservesExistingFavoriteAndSuccessClearsIt() {
        val failed = mangaFavoriteMutationResult(
            HttpText(code = 503, body = """{"error":"暂不可用"}"""),
            adding = false,
            wasFavorited = true,
        )
        assertFalse(failed.success)
        assertTrue(failed.favorited)
        assertEquals("收藏操作失败：暂不可用", failed.message)

        val deleted = mangaFavoriteMutationResult(
            HttpText(code = 200, body = """{"ok":true}"""),
            adding = false,
            wasFavorited = true,
        )
        assertTrue(deleted.success)
        assertFalse(deleted.favorited)
    }
}
