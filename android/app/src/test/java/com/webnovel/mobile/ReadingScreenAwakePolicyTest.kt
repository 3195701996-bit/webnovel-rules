package com.webnovel.mobile

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ReadingScreenAwakePolicyTest {
    @Test fun readerOnlyAddsForegroundRequest() {
        assertFalse(readingScreenAwake(false, false))
        assertTrue(readingScreenAwake(false, true))
        assertFalse(readingScreenAwake(false, false))
    }

    @Test fun existingRequestIsNeverClearedByBackgroundReader() {
        assertTrue(readingScreenAwake(true, true))
        assertTrue(readingScreenAwake(true, false))
    }
}
