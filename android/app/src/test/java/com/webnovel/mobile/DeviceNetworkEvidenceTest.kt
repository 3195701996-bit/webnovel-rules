package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Test

class DeviceNetworkEvidenceTest {
    @Test fun definiteDisconnectionIsOffline() {
        assertEquals("offline", deviceNetworkEvidence(false, null, null))
        assertEquals("offline", deviceNetworkEvidence(true, false, false))
    }

    @Test fun unknownOrUnvalidatedNetworkDoesNotBlockRequests() {
        assertEquals("", deviceNetworkEvidence(true, null, null))
        assertEquals("", deviceNetworkEvidence(true, true, false))
        assertEquals("", deviceNetworkEvidence(true, true, null))
    }

    @Test fun validatedWifiOrVpnRemainsOnlineRegardlessOfCellularMode() {
        assertEquals("online", deviceNetworkEvidence(true, true, true))
        assertEquals("online", deviceNetworkEvidence(true, true, true, radiosOff = true))
        assertEquals("", deviceNetworkEvidence(true, true, false, radiosOff = true))
        assertEquals("offline_confirmed", deviceNetworkEvidence(false, null, null, radiosOff = true))
    }
}
