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

    /** offline is advisory; offline_confirmed also has an explicit radios-off signal. */
    fun header(ctx: Context): String {
        // Airplane mode alone is not proof of disconnection: Wi-Fi (and VPN
        // over Wi-Fi) can legitimately remain online while cellular is off.
        val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE)
                as? ConnectivityManager ?: return ""
        val radiosOff = runCatching {
            Settings.Global.getInt(ctx.contentResolver, Settings.Global.AIRPLANE_MODE_ON, 0) == 1
        }.getOrDefault(false)
        val net = cm.activeNetwork ?: return deviceNetworkEvidence(false, null, null, radiosOff)
        val caps = cm.getNetworkCapabilities(net)
        return deviceNetworkEvidence(true,
            caps?.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET),
            caps?.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED), radiosOff)
    }
}
