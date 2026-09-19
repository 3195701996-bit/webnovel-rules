package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 漫画搜索延迟契约（真机）：**一个不可达源不得让用户干等满期限**。
 *
 * 背景：实测桌面实例上"每次搜索都正好 20.0s"——查明是一个不可达源（nhentai）拖满了
 * 自己的 20s 期限，而网页端早就改用流式端点绕开这件事，**原生 App 用的阻塞端点没有**。
 * 现在阻塞端点也带"耐心上限"：已有结果就先返回，慢源在 errors 里如实标注。
 *
 * 断言口径（避免把网络抖动当缺陷）：
 *   · 有结果时，响应必须在耐心上限附近返回（这里放宽到 15s 判"没有退化成等满期限"）；
 *   · errors 里的文案必须是两种诚实形态之一（"仍在查询…"或"搜索超时…"），
 *     不允许静默当成"没有结果"；
 *   · 同一查询的连续第二次必须命中缓存（秒回）。
 */
@RunWith(AndroidJUnit4::class)
class MangaSearchLatencyTest {

    private lateinit var gateway: EngineGateway

    private fun ev(line: String) = println("MANGA_SEARCH_LATENCY_EVIDENCE $line")

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

    @Test
    fun slowSource_doesNotBlockTheWholeSearch() = runBlocking {
        val ep = gateway.currentEndpoint()!!
        val q = "巨人"

        val t1 = System.currentTimeMillis()
        val first = gateway.httpText(ep.port, "/api/manga/search?q=" + java.net.URLEncoder.encode(q, "UTF-8"))
        val ms1 = System.currentTimeMillis() - t1
        assertTrue("搜索应 200：HTTP ${first.code}", first.ok)
        val o = JSONObject(first.body)
        val results = o.optJSONArray("results")?.length() ?: 0
        val errors = o.optJSONObject("errors")
        val errKeys = errors?.keys()?.asSequence()?.toList() ?: emptyList()
        ev("首次搜索 ${ms1}ms：结果 $results 条，未返回源 ${errKeys}")

        // 1) 有结果时不得退化成"等满 20s 期限"
        if (results > 0) {
            assertTrue("有结果时应在耐心上限附近返回（实际 ${ms1}ms）", ms1 < 15_000)
        }
        // 2) 未返回的源必须如实标注，且文案是两种诚实形态之一
        for (k in errKeys) {
            val why = errors!!.optString(k)
            assertTrue("未返回源的说明必须可读：$k → $why", why.isNotBlank())
            // 三种诚实形态都算合格：仍在查询 / 搜索超时 / 搜索失败（源站异常或限流）。
            // 真正要禁止的是"静默当成没有结果"——即错误为空或谎称成功。
            assertTrue("未返回源的说明必须是可读的失败/超时/仍在查询之一：$k → $why",
                why.contains("仍在查询") || why.contains("超时") || why.contains("失败"))
        }
        if (errKeys.isNotEmpty()) ev("如实标注：" + errKeys.joinToString("、") { k -> k + "=" + errors!!.optString(k).take(28) })

        // 3) 连续第二次必须秒回（部分结果也有短缓存）
        val t2 = System.currentTimeMillis()
        val second = gateway.httpText(ep.port,
            "/api/manga/search?q=" + java.net.URLEncoder.encode(q, "UTF-8"))
        val ms2 = System.currentTimeMillis() - t2
        assertTrue("第二次搜索应 200", second.ok)
        assertTrue("连续重搜应命中缓存（实际 ${ms2}ms）", ms2 < 3_000)
        val o2 = JSONObject(second.body)
        if (errKeys.isNotEmpty()) {
            val e2 = o2.optJSONObject("errors")
            assertTrue("命中缓存也要保留未返回源说明（不许假装全部成功）",
                (e2?.length() ?: 0) > 0)
        }
        ev("第二次搜索 ${ms2}ms：cached=${o2.optBoolean("cached")}，errors 保留=${
            (o2.optJSONObject("errors")?.length() ?: 0) > 0}")
    }
}
