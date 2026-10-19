package com.webnovel.mobile

import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.input.pointer.PointerEventPass
import androidx.compose.ui.input.pointer.PointerInputScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

internal fun isReaderTapDuration(pressedAt: Long, releasedAt: Long, longPressTimeout: Long): Boolean =
    longPressTimeout > 0 && releasedAt >= pressedAt && releasedAt - pressedAt < longPressTimeout

/** Observe taps without stealing the scrollable child's DOWN or consuming its drags. */
internal suspend fun PointerInputScope.detectContinuousReaderTaps(
    onTap: (Offset) -> Unit,
    onDoubleTap: () -> Unit,
) = coroutineScope {
    var pendingTap: Job? = null
    var previousUpTime = Long.MIN_VALUE
    var previousPosition = Offset.Zero
    awaitEachGesture {
        val down = awaitFirstDown(requireUnconsumed = false, pass = PointerEventPass.Initial)
        var dragged = false
        var released = false
        var position = down.position
        var upTime = down.uptimeMillis
        do {
            val event = awaitPointerEvent(PointerEventPass.Final)
            val change = event.changes.firstOrNull { it.id == down.id }
            if (event.changes.size != 1 || change == null) dragged = true
            if (change == null) break
            if ((change.position - down.position).getDistance() > viewConfiguration.touchSlop) {
                dragged = true
            }
            position = change.position
            upTime = change.uptimeMillis
            released = !change.pressed
            // A child button owns its release; do not also toggle the reader bars.
            if (released && change.isConsumed) dragged = true
        } while (!released)
        if (!released || dragged || !isReaderTapDuration(
                down.uptimeMillis, upTime, viewConfiguration.longPressTimeoutMillis)) {
            pendingTap?.cancel()
            previousUpTime = Long.MIN_VALUE
            return@awaitEachGesture
        }
        val interval = upTime - previousUpTime
        if (pendingTap?.isActive == true &&
            interval in viewConfiguration.doubleTapMinTimeMillis..viewConfiguration.doubleTapTimeoutMillis &&
            (position - previousPosition).getDistance() <= viewConfiguration.touchSlop * 2) {
            pendingTap?.cancel()
            previousUpTime = Long.MIN_VALUE
            onDoubleTap()
        } else {
            pendingTap?.cancel()
            previousUpTime = upTime
            previousPosition = position
            pendingTap = launch {
                delay(viewConfiguration.doubleTapTimeoutMillis)
                onTap(position)
            }
        }
    }
}
