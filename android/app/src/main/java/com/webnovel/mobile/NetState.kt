package com.webnovel.mobile

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.provider.Settings

/**
 * 本机联网状态 —— 交给本机引擎当**设备级辅助证据**（见 engine/neterr.py 与
 * server/state.device_offline_hint）。
 *
 * 为什么要它：断网时每个源都会失败，服务端只能从"某个源连不上"去猜是不是本机
 * 没网；而"目标不可达 / 某个域名解析不了"与"整机没有网络"是两件事，猜错就会给出
 * 错误的结论（2026-09-15 评审 P1-网络归因）。Android 这边其实**知道**答案，
 * 所以由 App 直接把状态告诉引擎，引擎只把它当证据之一，不用它否定真实失败。
 *
 * 语义刻意保守：
 *   · 没有活动网络 → offline（设备级确定）；
 *   · 有活动网络但没有 INTERNET 能力 → offline；
 *   · 有 INTERNET 但未通过验证（强制门户/受限网络）→ 不下结论（返回空，不发头）；
 *   · 其余 → online。
 * 这个头只影响服务端的**措辞与是否继续等待**，不决定能不能搜索、能不能阅读。
 */
object NetState {

    /** "offline" / "online" / ""（未知，不发这个头） */
    fun header(ctx: Context): String {
        // 飞行模式切换期间 ConnectivityManager 可能仍报告刚失效的 validated 网络；
        // 系统标志本身是明确的设备级断网证据。
        if (runCatching {
                Settings.Global.getInt(
                    ctx.contentResolver,
                    Settings.Global.AIRPLANE_MODE_ON,
                    0,
                ) == 1
            }.getOrDefault(false)
        ) return "offline"
        val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE)
                as? ConnectivityManager ?: return ""
        val net = cm.activeNetwork ?: return "offline"
        val caps = cm.getNetworkCapabilities(net) ?: return ""
        if (!caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)) {
            return "offline"
        }
        return if (caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)) {
            "online"
        } else {
            ""      // 有网络但未验证：可能是强制门户，拿不准就不说
        }
    }
}
