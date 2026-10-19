package com.webnovel.mobile

/** Preserve a pre-existing owner request; this reader only adds its foreground lease. */
internal fun readingScreenAwake(previousRequest: Boolean, readerResumed: Boolean): Boolean =
    previousRequest || readerResumed
