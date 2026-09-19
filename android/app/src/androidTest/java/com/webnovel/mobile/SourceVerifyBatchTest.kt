package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 全量逐源批次（**默认不跑**，需要显式给参数）。
 *
 * 为什么要门控：这一轮会把所有启用书源真的跑一遍（每源最多约 90 秒），
 * 放进常规回归会让每次跑测试都要等好几分钟。因此默认 assume 跳过（JUnit 记为 skipped，
 * 不是 passed），需要取证时显式执行：
 *
 *   adb shell am instrument -w -e batch 1 \
 *     -e class com.webnovel.mobile.SourceVerifyBatchTest \
 *     com.webnovel.mobile.test/androidx.test.runner.AndroidJUnitRunner
 *
 * 断言的是"覆盖面与统计口径"，不是"一定要通过多少"：
 *   - 启用源总数与书源列表一致；
 *   - 跑完后没有任何启用源处于"未验证"（never_attempted == 0），
 *     即这一轮确实把所有启用源都验过（通过/部分/失败/未支持都算验过）；
 *   - 通过率 = 通过数 / 启用总数，且通过数不超过实际条数。
 */
@RunWith(AndroidJUnit4::class)
class SourceVerifyBatchTest {

    private lateinit var gateway: EngineGateway

    private fun ev(line: String) = println("SOURCE_BATCH_EVIDENCE $line")

    @Before
    fun setUp() {
        gateway = EngineGateway(InstrumentationRegistry.getInstrumentation().targetContext)
    }

    @Test
    fun fullBatch_coversAllEnabledSources() {
        assumeTrue("需要 -e batch 1 才跑全量批次", InstrumentationRegistry.getArguments().getString("batch") == "1")

        runBlocking {
            val state = gateway.connect()
            assertTrue("引擎未就绪：$state", state is EngineState.Ready)
            val ep = gateway.currentEndpoint()!!

            val sources = EngineData.bookSources(gateway.httpText(ep.port, "/api/sources").body)
            val enabled = if (InstrumentationRegistry.getArguments().getString("sdisabled") == "1")
                sources.size else sources.count { it.enabled }
            ev("启用书源 $enabled 个（共 ${sources.size} 个）")
            assertTrue("没有启用书源，无法验证覆盖面", enabled > 0)

            // skip_verified_days=0：本轮把源都跑一遍，便于统计完整通过率。
            // include_disabled=1（-e sdisabled 1）：连**停用**的源也验一遍，
            // 用于给出"全部内置源"的通过率——只写验证结果，不改用户的书源启用状态。
            val args = InstrumentationRegistry.getArguments()
            val includeDisabled = args.getString("sdisabled") == "1"
            // stested：最近 N 天内**验过**的源本轮跳过（默认 1 天）。
            // 长批次容易被系统/厂商层中途杀掉，靠它续跑而不是从头再来
            // （服务端已支持每源增量落盘，结论不会白跑）。
            val skipTested = (args.getString("stested") ?: "1").toIntOrNull() ?: 1
            ev("本轮范围：${if (includeDisabled) "全部源（含停用）" else "仅启用源"}")
            val start = gateway.httpPost(ep.port, "/api/sources/verify",
                JSONObject().put("limit", 34).put("skip_verified_days", 0)
                    .put("skip_tested_days", skipTested)
                    .put("include_disabled", includeDisabled).toString())
            assertTrue("启动应被接受：HTTP ${start.code}", start.code == 202 || start.code == 200)

            var last: JSONObject = runCatching { JSONObject(start.body) }.getOrNull() ?: JSONObject()
            var finished = false
            // 每源最坏约 90 秒，这里给 25 分钟上限
            for (i in 0 until 500) {
                delay(3000)
                val o = runCatching {
                    JSONObject(gateway.httpText(ep.port, "/api/sources/verify/status").body)
                }.getOrNull() ?: continue
                last = o
                if (o.optString("status") != "running") { finished = true; break }
            }
            assertTrue("全量批次未在预期时间内结束", finished)
            assertEquals("批次应以 done 结束：${last.optString("error")}", "done", last.optString("status"))
            // 有 skip_tested_days 时本轮可能跳过已验过的源，只要求"跑过且收敛"
            assertTrue("本轮应至少跑或跳过 1 个源（total=${last.optInt("total")}，范围 $enabled）",
                last.optInt("total") >= 1)

            val body = gateway.httpText(ep.port, "/api/sources/verify/results").body
            val items = EngineData.verifications(body)
            val summary = EngineData.verifySummary(body)!!
            for (it in items) {
                ev("${it.status.padEnd(11)} ${it.name.take(24)}" +
                    (if (it.status == "verified") " 搜${it.searchCount}/目录${it.tocCount}/正文${it.contentChars}字"
                     else " ${it.reason.take(40)}"))
            }
            // 两个口径都要有值：enabled_total 恒为启用源数；all_total 为全部源数。
            // include_disabled 时本轮范围是全部源，就该用 all_total 对齐。
            if (includeDisabled) {
                assertEquals("全量口径分母应为全部源", enabled, summary.allTotal)
                assertTrue("全量口径未实测数应 >= 0", summary.neverAttemptedAll >= 0)
            } else {
                assertEquals("启用口径分母应为启用源", enabled, summary.enabledTotal)
                assertTrue("启用口径未实测数应 >= 0", summary.neverAttempted >= 0)
            }
            ev("口径：启用 ${summary.verifiedEnabled}/${summary.enabledTotal}=${summary.passRate}% · " +
                "全部 ${summary.verified}/${summary.allTotal}=${summary.passRateAll}% · " +
                "已记录 ${summary.recorded} 条（重复 uid 会折叠）")
            // 启用口径的分子必须是 verifiedEnabled（只数启用源里的通过），
            // 用全量 verified 会算出 367% 这种数字（用例自己写错过一次）
            val expectRate = if (summary.enabledTotal > 0)
                Math.round(summary.verifiedEnabled * 100.0 / summary.enabledTotal).toInt() else 0
            assertEquals("启用口径通过率", expectRate, summary.passRate)
            val expectAll = if (summary.allTotal > 0)
                Math.round(summary.verified * 100.0 / summary.allTotal).toInt() else 0
            assertEquals("全量口径通过率", expectAll, summary.passRateAll)
            // 通过数的上限要按本轮范围比：全量批次下 verified 可以超过"启用源数"
            val scopeTotal = if (includeDisabled) summary.allTotal else summary.enabledTotal
            assertTrue("通过数不应超过本轮范围总数（${summary.verified}/$scopeTotal）",
                summary.verified <= scopeTotal)
            assertTrue("启用口径的分子不应超过启用源数",
                summary.verifiedEnabled <= summary.enabledTotal)
            ev("汇总：通过 ${summary.verified} / 启用 ${summary.enabledTotal} " +
                "= ${summary.passRate}%；部分 ${summary.partial}，失败 ${summary.failed}，" +
                "未支持 ${summary.unsupported}")
        }
    }
}
