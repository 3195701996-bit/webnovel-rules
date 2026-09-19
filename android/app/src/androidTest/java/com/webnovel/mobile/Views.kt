package com.webnovel.mobile

import android.app.Activity
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import org.junit.Assert.assertEquals

/**
 * 视图层证据：核心流程**不得依赖 WebView**（方向基线 §6.3 / §8.A）。
 *
 * 为什么需要它：只在 Compose 语义树里找几个桌面特征串，**看不到 WebView 的内部 DOM**
 * ——网页里换成别的文案就查不出来了（2026-09-15 评审对 webMarkerHits() 的意见）。
 * 这里直接遍历 Activity 的视图树，数 WebView 实例：核心目的地必须为 0。
 * 设置里的"网页诊断"页是唯一例外，故不在本断言的覆盖范围内。
 */
object Views {

    fun webViewCount(activity: Activity): Int {
        var n = 0
        fun walk(v: View?) {
            if (v == null) return
            if (v is WebView) n++
            if (v is ViewGroup) for (i in 0 until v.childCount) walk(v.getChildAt(i))
        }
        walk(activity.window?.decorView)
        return n
    }

    fun assertNoWebView(activity: Activity, where: String) {
        assertEquals("$where 不得创建 WebView（核心流程不套网页）",
            0, webViewCount(activity))
    }
}
