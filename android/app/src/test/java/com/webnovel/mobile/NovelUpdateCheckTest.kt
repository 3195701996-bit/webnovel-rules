package com.webnovel.mobile

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 小说「检查更新」文案与缺失数（GET /api/books/<key>/check-status）。
 *
 * 为什么要有这个用例：这段文案是用户判断「要不要补下载」的**唯一依据**，
 * 而且真实踩过两次：
 *   1. 缺失数曾用 `new_chapters + retry_failed` 计算，漏掉「本地未下载」那一类 ——
 *      一本下到一半的书（50/1306 章）显示「已是最新（无新增/失败章节）」、
 *      按钮写「下载缺失章节（0）」，而服务端其实有 1256 章可补；
 *   2. 目录没取全 / 源站不可达时，服务端 message 已如实说明，界面若吞掉就变成
 *      「已是最新」——等于对用户撒谎。
 *
 * 判据与漫画侧 MangaUpdateCheckParseTest 同一原则：
 * **绝不把「检查失败 / 没查成」说成「已是最新」**。
 * 本文件是 **JVM 单测**（`./gradlew :app:testDebugUnitTest`，不需要设备）：
 * 纯逻辑不该只能在真机上验——改一次文案就得等插线才能验证，实测吃过亏。
 */
class NovelUpdateCheckTest {

    private fun ev(line: String) = println("NOVEL_UPDATE_CHECK_EVIDENCE $line")

    private fun body(vararg pairs: Pair<String, Any?>): String {
        val o = JSONObject()
        for ((k, v) in pairs) o.put(k, v)
        return o.toString()
    }

    @Test
    fun halfDownloadedBookMustNotSayFresh() {
        val half = EngineData.novelUpdateCheck(body(
            "ok" to true, "status" to "done", "new_chapters" to 0, "retry_failed" to 0,
            "missing_count" to 1256, "message" to "发现 1256 个缺失章节"))
        assertEquals(1256, half.missingCount)
        assertTrue("必须说清有多少章待补：${half.label}", half.label.contains("1256"))
        assertFalse("不能谎称已是最新：${half.label}", half.label.contains("已是最新"))
        ev("半本：${half.label}")
    }

    @Test
    fun newChaptersAndFailedChaptersAreBothShown() {
        val mixed = EngineData.novelUpdateCheck(body(
            "ok" to true, "status" to "done", "new_chapters" to 7, "retry_failed" to 3,
            "missing_count" to 10, "message" to "发现 10 个缺失章节"))
        assertTrue("新章/失败/待补都要体现：${mixed.label}",
            mixed.label.contains("7") && mixed.label.contains("3") &&
                mixed.label.contains("10"))
        ev("新章+失败：${mixed.label}")
    }

    @Test
    fun trulyUpToDate() {
        val fresh = EngineData.novelUpdateCheck(body(
            "ok" to true, "status" to "done", "new_chapters" to 0, "retry_failed" to 0,
            "missing_count" to 0, "message" to "已是最新，无缺失章节"))
        assertEquals("已是最新（无新增/失败章节）", fresh.label)
    }

    @Test
    fun partialTocAndUnreachableSourceMustBeSurfaced() {
        val partial = EngineData.novelUpdateCheck(body(
            "ok" to true, "status" to "done", "new_chapters" to 0, "retry_failed" to 0,
            "missing_count" to 0,
            "message" to "已是最新，无缺失章节；目录未取全（撞分页上限），可能仍有新章未检出"))
        assertTrue("未取全必须透出：${partial.label}", partial.label.contains("未取全"))
        ev("未取全：${partial.label}")

        val unreachable = EngineData.novelUpdateCheck(body(
            "ok" to true, "status" to "done", "new_chapters" to 0, "retry_failed" to 0,
            "missing_count" to 0,
            "message" to "已是最新，无缺失章节；源站目录本次不可达，新章未核对"))
        assertTrue("不可达必须透出：${unreachable.label}", unreachable.label.contains("不可达"))
        ev("源站不可达：${unreachable.label}")
    }

    @Test
    fun failureIsNeverReportedAsFresh() {
        val failed = EngineData.novelUpdateCheck(body(
            "ok" to false, "status" to "error", "error" to "检查更新失败",
            "message" to "检查更新失败"))
        assertTrue("要显示失败原因：${failed.label}", failed.label.contains("检查失败"))
        assertFalse("失败不得被说成已是最新", failed.label.contains("已是最新"))

        val broken = EngineData.novelUpdateCheck("not json")
        assertFalse("解析失败不得说成已是最新", broken.label.contains("已是最新"))
        ev("解析失败：${broken.label}")
    }

    @Test
    fun legacyServerWithoutMissingCountFallsBack() {
        val legacy = EngineData.novelUpdateCheck(body(
            "ok" to true, "status" to "done", "new_chapters" to 4, "retry_failed" to 1))
        assertEquals(5, legacy.missingCount)
        assertFalse(legacy.hasMissingCount)
        assertTrue("老服务端回退后也要报出数量：${legacy.label}", legacy.label.contains("5"))
    }

    @Test
    fun checkingStatusIsNotTreatedAsDone() {
        // 界面据此继续轮询（慢源目录 60+ 页，实测可到 20s）；
        // 解析层要把 status 原样带出来，别自己「猜成完成」。
        val checking = EngineData.novelUpdateCheck(body(
            "ok" to true, "status" to "checking", "message" to "检查中…"))
        assertEquals("checking", checking.status)
    }
}
