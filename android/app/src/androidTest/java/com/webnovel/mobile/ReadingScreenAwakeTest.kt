package com.webnovel.mobile

import android.view.View
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.lifecycle.Lifecycle
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class ReadingScreenAwakeTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()

    @Test fun foregroundLeaseRestoresOnPauseAndReaderExit() {
        val automation = InstrumentationRegistry.getInstrumentation().uiAutomation
        val qemu = android.os.ParcelFileDescriptor.AutoCloseInputStream(
            automation.executeShellCommand("getprop ro.kernel.qemu"))
            .bufferedReader().use { it.readText().trim() }
        assertTrue("只允许专用模拟器运行测试界面", qemu == "1")
        val visible = mutableStateOf(true)
        lateinit var readerView: View
        rule.activityRule.scenario.onActivity { activity ->
            activity.setContent {
                readerView = LocalView.current
                if (visible.value) ReadingScreenAwake()
            }
        }
        rule.runOnIdle { assertTrue("前台阅读应请求常亮", readerView.keepScreenOn) }
        rule.activityRule.scenario.moveToState(Lifecycle.State.STARTED)
        InstrumentationRegistry.getInstrumentation().runOnMainSync {
            assertFalse("暂停阅读页必须释放自身请求", readerView.keepScreenOn)
        }
        rule.activityRule.scenario.moveToState(Lifecycle.State.RESUMED)
        rule.runOnIdle { assertTrue("回到阅读页应恢复常亮", readerView.keepScreenOn) }
        rule.runOnIdle { visible.value = false }
        rule.runOnIdle { assertFalse("离开阅读页必须恢复原值", readerView.keepScreenOn) }
    }
}
