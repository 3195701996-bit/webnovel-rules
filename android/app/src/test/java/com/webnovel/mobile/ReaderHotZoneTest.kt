package com.webnovel.mobile

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ReaderHotZoneTest {
    @Test fun hotspotStaysSmallInPortraitAndLandscape() {
        for ((width, height) in listOf(390f to 844f, 844f to 390f)) {
            assertTrue(isReaderControlHotZone(width * .5f, height * .5f, width, height))
            assertTrue(isReaderControlHotZone(width * .39f, height * .43f, width, height))
            assertTrue(isReaderControlHotZone(width * .61f, height * .57f, width, height))
            for ((x, y) in listOf(.38f to .5f, .62f to .5f, .5f to .42f, .5f to .58f)) {
                assertFalse(isReaderControlHotZone(width * x, height * y, width, height))
            }
        }
    }

    @Test fun invalidLayoutNeverRevealsControls() {
        assertFalse(isReaderControlHotZone(0f, 0f, 0f, 0f))
        assertFalse(isReaderControlHotZone(0f, 0f, -1f, 100f))
        assertFalse(isReaderControlHotZone(Float.POSITIVE_INFINITY, 50f, Float.POSITIVE_INFINITY, 100f))
        assertFalse(isReaderControlHotZone(50f, Float.NaN, 100f, 100f))
    }
}
