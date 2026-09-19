package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.net.URLEncoder

/**
 * **候选源探针（0.51.0 路线 §7 P1-3 选源证据，非回归断言）**
 *
 * 目的：在**设备上**（真实内核 + 真实网络）实测候选小说源的 `搜索` 与 `目录` 两步，
 * 用当前实测结果挑选「最小回归源集」，而不是照抄历史记录（历史记录可能是 skipped/陈旧）。
 *
 * 约束：
 *   · 只做只读探测（/api/sources、/api/search、/api/search-toc），**不建任务、不下载、不删数据**；
 *   · 不断言成败——它产出的是证据，回归断言在 NovelRegressionTest；
 *   · 每源关键词按命中概率排序，命中即停，避免无谓请求。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class NovelSourceProbeTest {

    private val candidates = listOf(
        "爱下书_ixdzs8__ixdzs8.com",
        "精华书阁_m.jhsssd.com_",
        "quanben.io",
        "quanben8.com",
        "quanbenw.com",
        "quanbenxiaoshuo.com",
        "qb5200.org",
        "81zw.org",
        "kcshu.com",
        "www.yuzhaiwuh.xyz",
        "啃书网_kenshuwx__www.kenshuzw.la",
        "大帝书阁wap_ddsk2__wap.dingdiansk.com",
        "大帝书阁wap_ddsk__wap.ddsk.la",
        "思路客_isiluke_la__www.isiluke.la",
        "笔趣阁新_xinbqg__www.xinbqg.org",
        "顶点_m__m.23uswx.la",
        "爱下书_aixiaxsw__www.aixiaxsw.com",
    )

    private val keywords = listOf("剑来", "斗破苍穹")

    private fun ev(line: String) = println("NOVEL_PROBE_EVIDENCE $line")

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8")

    /**
     * 扫源用例必须**参数门控**（约定见 NovelCatalogTest）：默认全量套件里跳过。
     *
     * 为什么：这一条会连续真打十几到几十个小说源，既可能触发源站限流，
     * 也会长时间占住引擎（内存/线程持续增长）。实测在 0.67.0 的全量套件里，
     * 跑到这一条时**应用进程直接崩溃**，导致后面十几个类根本没跑 —— 全量套件的
     * 结果因此不完整。要跑它时显式加参数：
     *
     *   adb shell am instrument -w -e novelSweep 1 \
     *     com.webnovel.mobile.test/androidx.test.runner.AndroidJUnitRunner
     */
    private val sweepEnabled: Boolean
        get() = (InstrumentationRegistry.getArguments()
            .getString("novelSweep") ?: "") in listOf("1", "true", "yes")

    @Test
    fun probeCandidateNovelSources_searchAndToc(): Unit = runBlocking {
        assumeTrue("扫源用例默认跳过（加 -e novelSweep 1 才跑）", sweepEnabled)
        val gw = EngineGateway(ctx)
        val ep = (gw.connect() as? EngineState.Ready)?.endpoint
            ?: throw AssertionError("引擎未就绪")

        // 设备上真实的书源清单（enabled 状态只相信设备）
        val enabled = HashSet<String>()
        val present = HashSet<String>()
        val sr = gw.httpText(ep.port, "/api/sources")
        if (sr.ok) {
            val arr = JSONObject(sr.body).optJSONArray("sources") ?: org.json.JSONArray()
            for (i in 0 until arr.length()) {
                val s = arr.optJSONObject(i) ?: continue
                val uid = s.optString("uid")
                present.add(uid)
                if (s.optBoolean("enabled", false)) enabled.add(uid)
            }
        }
        ev("设备书源：共 ${present.size} 个，启用 ${enabled.size} 个")

        var okSearch = 0
        var okToc = 0
        for (uid in candidates) {
            val state = when {
                uid !in present -> "不存在"
                uid in enabled -> "已启用"
                else -> "已停用"
            }
            var hit: Pair<String, String>? = null
            var usedKw = ""
            var detail = ""
            for (kw in keywords) {
                val r = runCatching {
                    gw.httpText(ep.port, "/api/search?source=${enc(uid)}&q=${enc(kw)}")
                }.getOrElse { detail = "异常 ${it.message}"; null } ?: continue
                if (!r.ok) { detail = "HTTP ${r.code} ${r.body.take(60)}"; continue }
                val o = runCatching { JSONObject(r.body) }.getOrNull() ?: continue
                if (o.has("error")) detail = o.optString("error").take(60)
                val groups = o.optJSONArray("groups") ?: continue
                var count = 0
                for (i in 0 until groups.length()) {
                    val g = groups.optJSONObject(i) ?: continue
                    val ss = g.optJSONArray("sources") ?: continue
                    for (j in 0 until ss.length()) {
                        val s = ss.optJSONObject(j) ?: continue
                        if (s.optString("source_uid") == uid) {
                            count++
                            if (hit == null) {
                                hit = s.optString("book_url") to g.optString("name")
                                usedKw = kw
                            }
                        }
                    }
                }
                if (hit != null) break
                detail = "「$kw」0 结果"
            }
            if (hit == null) {
                ev("✗ $uid [$state] 搜索无结果（$detail）")
                delay(300)
                continue
            }
            okSearch++
            val (bookUrl, bookName) = hit
            val body = JSONObject().put("source_uid", uid).put("book_url", bookUrl).toString()
            val tr = runCatching { gw.httpPost(ep.port, "/api/search-toc", body) }
                .getOrElse { null }
            val tocN = tr?.let { r ->
                runCatching { JSONObject(r.body).optJSONArray("chapters")?.length() ?: 0 }.getOrDefault(0)
            } ?: 0
            val tocOk = tr != null && tr.ok && tocN > 0
            if (tocOk) okToc++
            ev((if (tocOk) "✓" else "△") + " $uid [$state] 搜索 ok（「$usedKw」→《${bookName.take(14)}》）" +
                " → 目录 ${if (tocOk) "$tocN 章" else "失败 HTTP ${tr?.code} ${tr?.body?.take(60) ?: "异常"}"}")
            delay(300)
        }
        ev("探针汇总：候选 ${candidates.size} 个；搜索命中 $okSearch；目录可用 $okToc")
    }
}
