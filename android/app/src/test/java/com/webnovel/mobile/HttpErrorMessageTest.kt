package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Test

class HttpErrorMessageTest {
    @Test fun decodesStructuredUnicodeErrorInsteadOfShowingRawJson() {
        assertEquals(
            "漫画不存在/已下架",
            EngineData.httpErrorMessage("""{"error":"\u6f2b\u753b\u4e0d\u5b58\u5728/\u5df2\u4e0b\u67b6"}"""),
        )
    }

    @Test fun surfacesRecoverableHistoryAndFavoriteErrorsAsUserText() {
        assertEquals(
            "阅读历史格式异常，原数据已保留",
            EngineData.httpErrorMessage(
                """{"recoverable":true,"error":"阅读历史格式异常，原数据已保留"}""",
            ),
        )
        assertEquals(
            "漫画收藏无法解析，原数据已保留，已取消更新检查",
            EngineData.httpErrorMessage(
                """{"ok":false,"recoverable":true,"error":"漫画收藏无法解析，原数据已保留，已取消更新检查"}""",
            ),
        )
    }

    @Test fun preservesPlainTextAndUsesFallbackForUnknownJson() {
        assertEquals("DNS lookup failed", EngineData.httpErrorMessage("DNS lookup failed"))
        assertEquals("请求未成功", EngineData.httpErrorMessage("""{"status":"error"}"""))
    }

    @Test fun normalizesAndBoundsLongMessages() {
        assertEquals("one two", EngineData.httpErrorMessage("one\n  two"))
        assertEquals("abcd", EngineData.httpErrorMessage("abcdefgh", maxLength = 4))
    }
}
