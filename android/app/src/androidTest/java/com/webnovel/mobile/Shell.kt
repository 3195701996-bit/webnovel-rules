package com.webnovel.mobile

import android.os.ParcelFileDescriptor
import androidx.test.platform.app.InstrumentationRegistry

/**
 * 以 shell 身份执行命令（UiAutomation）：屏幕开关、Doze、飞行模式这类设备状态
 * 只能这样切。读空输出即等到命令结束。
 */
object Shell {
    fun run(cmd: String): String {
        val inst = InstrumentationRegistry.getInstrumentation()
        val pfd = inst.uiAutomation.executeShellCommand(cmd)
        return ParcelFileDescriptor.AutoCloseInputStream(pfd).use {
            it.readBytes().toString(Charsets.UTF_8)
        }
    }
}
