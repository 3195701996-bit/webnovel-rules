package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 书源「批量启用/停用」真机验收（路径级）。
 *
 * 为什么需要它：内置 34 个源的启用状态是长期一个个点出来的，手机端搜索只覆盖
 * 启用中的源；要"只启用验证通过的、停用失效的"必须能一次做完。
 *
 * 口径（不夸大）：
 *   - 只按**已记录的**功能验证结论筛选（没测过 ≠ 通过）；
 *   - 返回的 matched / changed / enabled_now 必须是真实条数，用**回读**核对；
 *   - 不带 uids 的未知 filter → 400；uids 里不存在的 uid → 如实进 missing。
 *
 * 本用例会真的改状态（这才叫验收），因此结束前**回滚**到初始启用集合，
 * 避免影响同一次运行里的其它用例与设备上的实际配置。
 */
@RunWith(AndroidJUnit4::class)
class SourceBulkPathTest {

    private lateinit var gateway: EngineGateway

    private fun ev(line: String) = println("SOURCE_BULK_EVIDENCE $line")

    @Before
    fun setUp() {
        gateway = EngineGateway(InstrumentationRegistry.getInstrumentation().targetContext)
        val state = runBlocking { gateway.connect() }
        assertTrue("引擎未就绪：$state", state is EngineState.Ready)
    }

    @After
    fun tearDown() {
        runCatching { runBlocking { gateway.stopEngine() } }
    }

    private suspend fun sourcesNow(port: Int): List<BookSource> {
        val r = gateway.httpText(port, "/api/sources")
        assertTrue("读取书源失败：HTTP ${r.code}", r.ok)
        return EngineData.bookSources(r.body)
    }

    private suspend fun verifiedUids(port: Int): List<String> {
        val r = gateway.httpText(port, "/api/sources/verify/results")
        if (!r.ok) return emptyList()
        return EngineData.verifications(r.body)
            .filter { it.status == "verified" }.map { it.uid }
    }

    private fun uidsBody(enabled: Boolean, uids: List<String>): String =
        JSONObject().put("enabled", enabled).put("uids", JSONArray(uids)).toString()

    @Test
    fun bulkEnabled_isHonestAndReversible() = runBlocking {
        val ep = gateway.currentEndpoint()!!
        val before = sourcesNow(ep.port)
        fun enabledUids(l: List<BookSource>) = l.filter { it.enabled }.map { it.uid }.toSet()
        val beforeUids = enabledUids(before)
        val beforeEntries = before.count { it.enabled }
        ev("初始：${before.size} 个源文件，启用 ${beforeUids.size} 个唯一源（$beforeEntries 个文件）")

        // 1) 未知 filter → 400（不许静默当成 all）
        val bad = gateway.httpPost(ep.port, "/api/sources/bulk-enabled",
            JSONObject().put("enabled", true).put("filter", "zzz").toString())
        assertEquals("未知 filter 应 400", 400, bad.code)
        ev("未知 filter → HTTP ${bad.code}")

        // 2) 不存在的 uid → matched=0 且进 missing（如实回报，不假装成功）
        val miss = gateway.httpPost(ep.port, "/api/sources/bulk-enabled",
            uidsBody(true, listOf("__不存在的源__")))
        assertTrue("应 200 但 matched=0：${miss.code}", miss.ok)
        val mo = JSONObject(miss.body)
        assertEquals("不存在 uid 不应算匹配", 0, mo.optInt("matched"))
        assertEquals("不存在 uid 不应产生改动", 0, mo.optInt("changed"))
        assertTrue("不存在 uid 必须进 missing",
            (mo.optJSONArray("missing")?.length() ?: 0) > 0)
        ev("不存在 uid：matched=0 changed=0 missing=" +
            (mo.optJSONArray("missing")?.length() ?: 0))

        // 3) 恒等调用（filter=enabled + enabled=true）：matched>0、changed=0，
        //    两种口径（唯一源 / 文件）都要与会话开头一致——顺便验证数字没混用
        val noop = gateway.httpPost(ep.port, "/api/sources/bulk-enabled",
            JSONObject().put("enabled", true).put("filter", "enabled").toString())
        assertTrue("恒等调用应 200：HTTP ${noop.code}", noop.ok)
        val no = JSONObject(noop.body)
        ev("恒等调用：matched=${no.optInt("matched")} changed=${no.optInt("changed")} " +
            "enabled_now=${no.optInt("enabled_now")} enabled_entries=${no.optInt("enabled_entries")}")
        assertEquals("恒等调用不应产生改动", 0, no.optInt("changed"))
        assertEquals("matched 应等于启用中的唯一源数", beforeUids.size, no.optInt("matched"))
        assertEquals("唯一源口径应与回读一致", beforeUids.size, no.optInt("enabled_now"))
        assertEquals("文件口径应与回读一致", beforeEntries, no.optInt("enabled_entries"))
        assertEquals("文件总数口径应与回读一致", before.size, no.optInt("total_entries"))
        assertTrue("恒等调用必须说明原因", no.optString("note").isNotBlank())

        // 4) 真改一个源：挑一个"当前停用且只有单个文件"的 uid（改完立刻回滚），
        //    这一条才是真正验证"写进去了"——改完用回读确认，不看返回值自说自话
        val target = before.firstOrNull { b ->
            !b.enabled && before.count { it.uid == b.uid } == 1
        }
        assertTrue("应能找到一个单文件的停用源用于验证", target != null)
        val tu = target!!.uid
        try {
            val on = gateway.httpPost(ep.port, "/api/sources/bulk-enabled", uidsBody(true, listOf(tu)))
            assertTrue("启用单个源应 200：HTTP ${on.code}", on.ok)
            val oo = JSONObject(on.body)
            assertEquals("应恰好匹配 1 个源", 1, oo.optInt("matched"))
            assertEquals("应恰好改动 1 个文件", 1, oo.optInt("changed"))
            assertEquals("启用后唯一源口径应 +1", beforeUids.size + 1, oo.optInt("enabled_now"))
            val after = sourcesNow(ep.port)
            assertTrue("回读应看到该源已启用：$tu", after.first { it.uid == tu }.enabled)
            ev("单源启用：$tu → 回读 enabled=true，enabled_now=${oo.optInt("enabled_now")}")
        } finally {
            // 无论上面断言是否失败都要回滚，避免把设备上的实际配置留在改动后的状态
            val off = gateway.httpPost(ep.port, "/api/sources/bulk-enabled",
                uidsBody(false, listOf(tu)))
            ev("回滚该源：HTTP ${off.code} changed=${JSONObject(off.body).optInt("changed")}")
        }

        // 5) 终态必须与初始态完全一致（唯一源集合与文件数都不许漂移）
        val restored = sourcesNow(ep.port)
        assertEquals("终态启用集合应与初始一致", beforeUids, enabledUids(restored))
        assertEquals("终态启用文件数应与初始一致", beforeEntries, restored.count { it.enabled })
        assertEquals("终态源文件数应与初始一致", before.size, restored.size)
        ev("终态一致：启用 ${restored.count { it.enabled }} 个文件，" +
            "${enabledUids(restored).size} 个唯一源")
    }
}
