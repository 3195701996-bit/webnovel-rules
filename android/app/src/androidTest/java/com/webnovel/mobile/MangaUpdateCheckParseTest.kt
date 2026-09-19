package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 漫画更新检查结果的解析（离线、不需要引擎）：字段缺失要按"未知/失败"处理，
 * 绝不把"检查失败"说成"已是最新"——这是界面文案的唯一来源，必须钉住。
 */
@RunWith(AndroidJUnit4::class)
class MangaUpdateCheckParseTest {

    private fun ev(line: String) = println("MANGA_UPDATE_PARSE_EVIDENCE $line")

    @Test
    fun parse_isDefensiveAndHonest() {
        // 1) 有新话：missing 是对象数组（带 id），计数与最新话名都要读出来
        val withNew = EngineData.mangaUpdateCheck(
            """{"ok":true,"has_update":true,"missing_count":2,"latest":"第99话",
                "missing":[{"id":"ep99","name":"第99话"},{"id":"ep98","name":"第98话"}]}"""
        )
        assertTrue(withNew.ok); assertTrue(withNew.hasUpdate)
        assertEquals(2, withNew.missingCount)
        assertEquals(listOf("ep99", "ep98"), withNew.missingIds)
        assertTrue("标签应含新话数与最新话名", withNew.label.contains("2") && withNew.label.contains("第99话"))
        ev("有新话：${withNew.label} ids=${withNew.missingIds}")

        // 2) 已是最新：不得报成"发现 N 个新话"
        val latest = EngineData.mangaUpdateCheck("""{"ok":true,"has_update":false,"missing_count":0}""")
        assertTrue(latest.ok); assertFalse(latest.hasUpdate)
        assertEquals("已是最新", latest.label)
        ev("已是最新：${latest.label}")

        // 3) 检查失败：必须显示失败原因，不能显示"已是最新"
        val failed = EngineData.mangaUpdateCheck("""{"ok":false,"error":"源站超时"}""")
        assertFalse(failed.ok)
        assertTrue("失败标签要带原因：${failed.label}", failed.label.contains("源站超时"))
        assertFalse("失败不得被说成已是最新", failed.label.contains("已是最新"))
        ev("失败：${failed.label}")

        // 4) 字段缺失/响应异常：按未知处理，不猜成"已是最新"
        val broken = EngineData.mangaUpdateCheck("{ 这不是 JSON")
        assertFalse(broken.ok)
        assertTrue(broken.label.contains("失败"))
        ev("异常响应：${broken.label}")

        // 5) missing 为字符串数组时也能取到 id
        val strIds = EngineData.mangaUpdateCheck(
            """{"ok":true,"has_update":true,"missing":["ep7"]}""")
        assertEquals(listOf("ep7"), strIds.missingIds)
        assertEquals(1, strIds.missingCount)   // 缺 missing_count 时用 ids 数量兜底
        ev("字符串数组：${strIds.missingIds} count=${strIds.missingCount}")

        // 6) 书库批量检查：运行中/完成/结果为空 三种状态的标签与"有更新"筛选
        val running = EngineData.mangaLibraryUpdate(
            """{"running":true,"done":2,"total":5,"results":[{"title":"甲","ok":true,"has_update":false}]}""")
        assertTrue(running.running)
        assertTrue("运行中要显示进度：${running.label}", running.label.contains("2/5"))
        ev("批量运行中：${running.label}")

        val doneBody = """{"running":false,"done":3,"total":3,"results":[
            {"title":"甲","ok":true,"has_update":true,"missing_count":3},
            {"title":"乙","ok":true,"has_update":false,"missing_count":0},
            {"title":"丙","ok":false,"error":"源站超时"}]}"""
        val done = EngineData.mangaLibraryUpdate(doneBody)
        assertEquals(3, done.items.size)
        assertEquals("只有「有新话」的部才算有更新（失败的不算）", 1, done.withUpdate.size)
        assertEquals("甲", done.withUpdate[0].first)
        assertTrue("完成标签要带部数：${done.label}", done.label.contains("1") && done.label.contains("3"))
        ev("批量完成：${done.label} · ${done.items.map { it.first + "=" + it.second.label }}")

        // 7) 没有结果时不能显示"全部已是最新"（那是"尚未检查"）
        val empty = EngineData.mangaLibraryUpdate("""{"running":false,"done":0,"total":0,"results":[]}""")
        assertEquals("尚未检查", empty.label)
        assertFalse("空结果不得说成已是最新", empty.label.contains("已是最新"))
        ev("空结果：${empty.label}")
    }
}
