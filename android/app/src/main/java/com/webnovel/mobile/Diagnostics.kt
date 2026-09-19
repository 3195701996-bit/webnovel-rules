package com.webnovel.mobile

/**
 * 产物基线与本地诊断（方向基线 P0-A / P0-E）。
 *
 * 目的有两个：
 *  1. 让"装的是哪个包"可核对：版本号 + versionCode + git revision + 构建时间，
 *     用户截图能直接对上，不需要事后猜；
 *  2. 引擎起不来时**在这一页**就能看到诊断要点（设置页那时根本进不去）。
 */
object BuildInfo {
    /** 一行式产物标识，设置页与故障页共用同一份文本（避免两处漂移） */
    val summary: String
        get() = "版本 ${BuildConfig.VERSION_NAME}（${BuildConfig.VERSION_CODE}）" +
            " · 源码 ${BuildConfig.GIT_REVISION}（${BuildConfig.GIT_DIRTY}）" +
            " · 构建 ${BuildConfig.BUILD_TIME}"
}

object EngineDiagnostics {
    /** 引擎故障时的诊断文本：**不依赖引擎在线**，离线也能看 */
    fun text(st: EngineState.Failed): String = buildString {
        append(BuildInfo.summary).append('\n')
        append("系统：Android ").append(android.os.Build.VERSION.RELEASE)
        append("（API ").append(android.os.Build.VERSION.SDK_INT).append("）")
        append(" · ").append(android.os.Build.SUPPORTED_ABIS.firstOrNull() ?: "?").append('\n')
        append("引擎阶段：").append(st.stage).append('\n')
        append("错误：").append(st.detail).append('\n')
        append("连接方式：应用内回环 127.0.0.1（端口由网关自动挑选，无需手工配置）").append('\n')
        append('\n').append("自查要点：").append('\n')
        append("· 首次启动要解包内置引擎，通常十几秒；慢设备更久，先重试一次").append('\n')
        append("· 提示端口占用 → 重试即可（网关会重新挑端口）").append('\n')
        append("· 提示解包/写入失败 → 检查可用存储空间").append('\n')
        append("· 本页信息不需要联网；联网只影响搜索与下载").append('\n')
        append("· 书源配置与已下载内容在应用私有目录，重试不会清空").append('\n')
    }
}
