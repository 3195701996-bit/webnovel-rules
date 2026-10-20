package com.webnovel.mobile

import android.view.KeyEvent
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class MangaReaderKeyNavigationTest {
    @Test
    fun pagingKeysFollowTheActiveReadingDirection() {
        assertEquals(
            MangaReaderKeyAction.PREVIOUS_PAGE,
            mangaReaderKeyAction(KeyEvent.KEYCODE_DPAD_LEFT, paged = true),
        )
        assertEquals(
            MangaReaderKeyAction.NEXT_PAGE,
            mangaReaderKeyAction(KeyEvent.KEYCODE_DPAD_RIGHT, paged = true),
        )
        assertNull(mangaReaderKeyAction(KeyEvent.KEYCODE_DPAD_LEFT, paged = false))
        assertNull(mangaReaderKeyAction(KeyEvent.KEYCODE_DPAD_RIGHT, paged = false))
    }

    @Test
    fun pageAndChapterKeysWorkInEitherReadingMode() {
        for (paged in listOf(false, true)) {
            assertEquals(
                MangaReaderKeyAction.PREVIOUS_PAGE,
                mangaReaderKeyAction(KeyEvent.KEYCODE_PAGE_UP, paged),
            )
            assertEquals(
                MangaReaderKeyAction.NEXT_PAGE,
                mangaReaderKeyAction(KeyEvent.KEYCODE_PAGE_DOWN, paged),
            )
            assertEquals(
                MangaReaderKeyAction.PREVIOUS_CHAPTER,
                mangaReaderKeyAction(KeyEvent.KEYCODE_MOVE_HOME, paged),
            )
            assertEquals(
                MangaReaderKeyAction.NEXT_CHAPTER,
                mangaReaderKeyAction(KeyEvent.KEYCODE_MOVE_END, paged),
            )
        }
    }

    @Test
    fun dpadVerticalKeysScrollContinuousReadingOnly() {
        assertEquals(
            MangaReaderKeyAction.PREVIOUS_PAGE,
            mangaReaderKeyAction(KeyEvent.KEYCODE_DPAD_UP, paged = false),
        )
        assertEquals(
            MangaReaderKeyAction.NEXT_PAGE,
            mangaReaderKeyAction(KeyEvent.KEYCODE_DPAD_DOWN, paged = false),
        )
        assertNull(mangaReaderKeyAction(KeyEvent.KEYCODE_DPAD_UP, paged = true))
        assertNull(mangaReaderKeyAction(KeyEvent.KEYCODE_DPAD_DOWN, paged = true))
    }
}
