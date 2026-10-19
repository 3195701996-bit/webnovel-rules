package com.webnovel.mobile

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ReaderTapDurationTest {
    @Test fun onlyReleasesStrictlyBeforeLongPressThresholdAreTaps() {
        assertTrue(isReaderTapDuration(1000, 1000, 400))
        assertTrue(isReaderTapDuration(1000, 1399, 400))
        assertFalse(isReaderTapDuration(1000, 1400, 400))
        assertFalse(isReaderTapDuration(1000, 1800, 400))
    }

    @Test fun invalidTimeOrConfigurationDoesNotBecomeAnAccidentalTap() {
        assertFalse(isReaderTapDuration(1000, 999, 400))
        assertFalse(isReaderTapDuration(1000, 1000, 0))
        assertFalse(isReaderTapDuration(1000, 1000, -1))
    }
}
