package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 逐源功能验证的契约与**诚实性**验收（诊断 §6）：
 *
 *   - 结果接口给出结构化状态，且只可能是
 *     verified / partial / failed / unsupported / skipped，不会出现含糊值；
 *   - 核心不变式：**任何标记 verified 的条目，三个阶段都必须 ok 且带证据数字**
 *     （搜索结果数、章节数、正文字数）——只搜到书不等于能读；
 *   - 启动一次真实验证（limit=1）并轮询到结束：进度收敛、结果落库、状态属于允许集合；
 *   - 未验证的源不会被算成通过：counts 里出现的键只能是实际跑过的状态。
 *
 * 注意：本轮会真实访问已启用书源（这正是功能验证的意义），因此耗时可到分钟级。
 */
@RunWith(AndroidJUnit4::class)
class SourceVerifyPathTest {

    private lateinit var gateway: EngineGateway
    private val allowed = setOf("verified", "partial", "failed", "unsupported", "skipped")

    private fun ev(line: String) = println("SOURCE_VERIFY_EVIDENCE $line")

    @Before
    fun setUp() {
        gateway = EngineGateway(InstrumentationRegistry.getInstrumentation().targetContext)
        val state = runBlocking { gateway.connect() }
        assertTrue("引擎未就绪：$state", state is EngineState.Ready)
    }

    @Test
    fun verifyRun_isBoundedAndHonest() = runBlocking {
        val ep = gateway.currentEndpoint()!!

        // 1) 结果接口可用（可能还没有结果：那就是"未验证"）
        val r0 = gateway.httpText(ep.port, "/api/sources/verify/results")
        assertTrue("结果接口状态码=${r0.code}", r0.ok)
        val before = EngineData.verifications(r0.body)
        for (it in before) {
            assertTrue("出现不允许的状态：${it.status}", it.status in allowed)
        }
        ev("开始前：已有记录 ${before.size} 条，其中通过 ${before.count { it.ok }} 条")

        // 2) 启动一轮（只验证 1 个源，避免长时间占用设备网络）
        val start = gateway.httpPost(ep.port, "/api/sources/verify",
            JSONObject().put("limit", 1).toString())
        val sj = runCatching { JSONObject(start.body) }.getOrNull()
        assertTrue("启动请求应被接受：HTTP ${start.code} ${start.body.take(80)}",
            start.code == 202 || start.code == 200)
        assertTrue("应返回 started/already_running 之一",
            sj!!.optBoolean("started") || sj.optBoolean("already_running"))
        ev("启动：HTTP ${start.code} started=${sj.optBoolean("started")} " +
            "already=${sj.optBoolean("already_running")} total=${sj.optInt("total")}")

        // 3) 轮询到结束（有界：最多 4 分钟；单源的各阶段预算合计约 1.5 分钟）
        var last: JSONObject = sj!!
        var finished = false
        for (i in 0 until 80) {
            delay(3000)
            val st = gateway.httpText(ep.port, "/api/sources/verify/status")
            val o = runCatching { JSONObject(st.body) }.getOrNull() ?: continue
            last = o
            val status = o.optString("status")
            if (status != "running") {
                finished = true
                assertEquals("结束状态应为 done，实际=$status error=${o.optString("error")}",
                    "done", status)
                break
            }
            assertTrue("进度不应超过总数", o.optInt("done") <= o.optInt("total"))
        }
        assertTrue("验证未在预期时间内结束（仍在跑也是允许的，但本用例要求收敛）", finished)
        ev("结束：done=${last.optInt("done")}/${last.optInt("total")}")

        // 4) 结果落库并满足诚实性不变式
        val r1 = gateway.httpText(ep.port, "/api/sources/verify/results")
        assertTrue("结果接口状态码=${r1.code}", r1.ok)
        val items = EngineData.verifications(r1.body)
        assertTrue("本轮应至少写入 1 条结果", items.isNotEmpty())
        val raw = JSONObject(r1.body)
        val counts = raw.optJSONObject("counts") ?: JSONObject()
        for (k in counts.keys()) {
            assertTrue("counts 里出现不允许的状态：$k", k in allowed)
        }
        ev("结果：总数=${items.size} counts=${counts}")

        val arr = raw.optJSONArray("items")!!
        var verifiedCount = 0
        for (i in 0 until arr.length()) {
            val it = arr.optJSONObject(i)!!
            val status = it.optString("status")
            assertTrue("出现不允许的状态：$status", status in allowed)
            assertTrue("每条都应有验证时间", it.optString("tested_at").isNotBlank())
            if (status == "verified") {
                verifiedCount++
                // 核心不变式：三阶段都必须 ok，且证据数字为正
                val st = it.optJSONObject("stages")!!
                for (stage in listOf("search", "toc", "content")) {
                    val s = st.optJSONObject(stage)
                    assertTrue("verified 条目缺少 $stage 阶段：${it.optString("name")}", s != null)
                    assertTrue("verified 条目的 $stage 阶段必须 ok：${it.optString("name")}",
                        s!!.optBoolean("ok"))
                }
                assertTrue("verified 条目必须有搜索结果数",
                    st.optJSONObject("search")!!.optInt("count") > 0)
                assertTrue("verified 条目必须有章节数",
                    st.optJSONObject("toc")!!.optInt("count") > 0)
                assertTrue("verified 条目必须有正文字数",
                    st.optJSONObject("content")!!.optInt("chars") > 0)
            } else {
                assertTrue("非 verified 条目必须给出原因（不能只说没通过）",
                    it.optString("reason").isNotBlank())
            }
        }
        ev("诚实性不变式通过：verified=$verifiedCount 条，其余均带原因")

        // 5) 未验证 ≠ 通过：结果里没有出现过的源不会出现在 counts 里
        // org.json 的 keys() 是 Iterator，先转成序列再求和
        val countKeys = counts.keys().asSequence().toList()
        assertTrue("counts 总数应等于结果条数（counts=$counts 结果数=${items.size}）",
            countKeys.sumOf { counts.optInt(it) } == items.size)
        assertFalse("不应把没有结果的源算作 verified", items.isEmpty() && verifiedCount > 0)

        // 6) 汇总（通过率）：分母是启用源总数，未验证的必须单独列出，
        //    不能用"已记录条数"当分母把没验证过的源算成通过
        val summary = EngineData.verifySummary(r1.body)
        assertTrue("结果里应带 summary", summary != null)
        val enabled = EngineData.bookSources(gateway.httpText(ep.port, "/api/sources").body)
            .count { it.enabled }
        assertEquals("summary 的启用源总数应与书源列表一致", enabled, summary!!.enabledTotal)
        // 启用口径的分子是 verifiedEnabled（只数启用源里的通过）——用全量 verified
        // 会算出 >100% 的通过率（本轮双口径改造时踩到过）
        val expectRate = if (summary.enabledTotal > 0)
            Math.round(summary.verifiedEnabled * 100.0 / summary.enabledTotal).toInt() else 0
        assertEquals("启用口径通过率", expectRate, summary.passRate)
        assertTrue("通过率不应超过 100%", summary.passRate in 0..100 && summary.passRateAll in 0..100)
        assertTrue("未验证数应 >= 0 且不超过启用总数",
            summary.neverAttempted >= 0 && summary.neverAttempted <= summary.enabledTotal)
        assertTrue("通过数不应超过结果条数", summary.verified <= items.size)
        assertTrue("启用口径分子不应超过启用源数", summary.verifiedEnabled <= summary.enabledTotal)
        ev("汇总：${summary.label} 未验证=${summary.neverAttempted}")

        // 7) 分批不覆盖：再跑一小批（offset 前进），第一批的结论必须还在
        val uidsBefore = items.map { it.uid }.toSet()
        val start2 = gateway.httpPost(ep.port, "/api/sources/verify",
            JSONObject().put("limit", 1).put("offset", 1).put("skip_verified_days", 0).toString())
        assertTrue("第二批启动应被接受：HTTP ${start2.code}", start2.code == 202 || start2.code == 200)
        for (i in 0 until 80) {
            delay(3000)
            val o = runCatching {
                JSONObject(gateway.httpText(ep.port, "/api/sources/verify/status").body)
            }.getOrNull() ?: continue
            if (o.optString("status") != "running") break
        }
        val after = EngineData.verifications(
            gateway.httpText(ep.port, "/api/sources/verify/results").body)
        assertTrue("第二批后结果不应减少（历史结论被覆盖了）", after.size >= items.size)
        assertTrue("第一批的结论必须仍在结果里", after.map { it.uid }.containsAll(uidsBefore))
        ev("分批合并：第一批 ${uidsBefore.size} 条 → 现在 ${after.size} 条（未丢失）")
    }
}
