package com.webnovel.mobile

import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.ui.platform.LocalView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner

/** Window-local only: never changes system sleep settings or acquires a wake lock. */
@Composable
internal fun ReadingScreenAwake() {
    val view = LocalView.current
    val owner = LocalLifecycleOwner.current
    DisposableEffect(view, owner) {
        val previous = view.keepScreenOn
        fun update() {
            view.keepScreenOn = readingScreenAwake(previous,
                owner.lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED))
        }
        val observer = LifecycleEventObserver { _, _ -> update() }
        owner.lifecycle.addObserver(observer)
        update()
        onDispose {
            owner.lifecycle.removeObserver(observer)
            view.keepScreenOn = previous
        }
    }
}
