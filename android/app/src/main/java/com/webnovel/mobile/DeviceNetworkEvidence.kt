package com.webnovel.mobile

/** Only definite lack of an Internet-capable active network implies offline. */
internal fun deviceNetworkEvidence(active: Boolean, internet: Boolean?, validated: Boolean?,
                                   radiosOff: Boolean = false): String =
    when {
        radiosOff && (!active || internet == false) -> "offline_confirmed"
        !active || internet == false -> "offline"
        internet == true && validated == true -> "online"
        else -> "" // Unknown/captive/unvalidated: do not veto an actual request.
    }
