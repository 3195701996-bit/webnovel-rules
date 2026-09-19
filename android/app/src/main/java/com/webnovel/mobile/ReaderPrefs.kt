package com.webnovel.mobile

import android.content.Context

/** 阅读主题：正文与纸张配色成对出现，避免只改背景不改字色导致看不清 */
enum class ReaderTheme(val label: String) {
    Light("白"),
    Paper("护眼"),
    Dark("夜间"),
}

/**
 * 阅读设置。持久化在应用私有空间（SharedPreferences），
 * 因此引擎未就绪或重启都不会丢失用户的排版偏好。
 */
data class ReaderPrefs(
    val fontSizeSp: Int = 18,
    val lineHeightMul: Float = 1.9f,
    val theme: ReaderTheme = ReaderTheme.Paper,
    /** 漫画阅读方向：false=纵向连续（默认），true=横向翻页 */
    val horizontalPaging: Boolean = false,
) {
    companion object {
        const val MIN_FONT = 14
        const val MAX_FONT = 26
        const val MIN_LINE = 1.3f
        const val MAX_LINE = 2.6f

        private const val FILE = "reader_prefs"

        fun load(ctx: Context): ReaderPrefs {
            val sp = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)
            val theme = runCatching {
                ReaderTheme.valueOf(sp.getString("theme", ReaderTheme.Paper.name)!!)
            }.getOrDefault(ReaderTheme.Paper)
            return ReaderPrefs(
                fontSizeSp = sp.getInt("font", 18).coerceIn(MIN_FONT, MAX_FONT),
                lineHeightMul = sp.getFloat("line", 1.9f).coerceIn(MIN_LINE, MAX_LINE),
                theme = theme,
                horizontalPaging = sp.getBoolean("manga_paged", false),
            )
        }

        fun save(ctx: Context, p: ReaderPrefs) {
            ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit()
                .putInt("font", p.fontSizeSp.coerceIn(MIN_FONT, MAX_FONT))
                .putFloat("line", p.lineHeightMul.coerceIn(MIN_LINE, MAX_LINE))
                .putString("theme", p.theme.name)
                .putBoolean("manga_paged", p.horizontalPaging)
                .apply()
        }
    }
}
