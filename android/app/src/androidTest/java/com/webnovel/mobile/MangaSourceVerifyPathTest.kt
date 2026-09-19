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
 * 漫画源功能验证的契约与诚实性验收（对照诊断 §6）。
 *
 * 不变式：
 *   - 状态只允许 verified / partial / failed / unsupported / skipped；
 *   - **任何 verified 条目，三个阶段都必须 ok 且带正数证据**
 *     （搜索结果数、目录话数、图片张数、首图字节数 > 1000）；
 *   - 非 verified 必须给出原因；依赖缺失的源必须标 unsupported 而不是"跑过但失败"；
 *   - `/api/manga/sources` 的 verified 字段必须与实测结论一致（不再恒为 false）。
 *
 * 默认只跑 1 个源（真实网络）；全量批次用 `-e mbatch 1` 显式开启。
 */
@RunWith(AndroidJUnit4::class)
class MangaSourceVerifyPathTest {

    private lateinit var gateway: EngineGateway
    private val allowed = setOf("verified", "partial", "failed", "unsupported", "skipped")

    private fun ev(line: String) = println("MANGA_VERIFY_EVIDENCE $line")

    @Before
    fun setUp() {
        gateway = EngineGateway(InstrumentationRegistry.getInstrumentation().targetContext)
    }

    @Test
    fun mangaVerifyRun_isHonestAndConsistent() {
        runBlocking {
            val state = gateway.connect()
            assertTrue("引擎未就绪：$state", state is EngineState.Ready)
            val ep = gateway.currentEndpoint()!!

            // 能力台账：漫画源列表必须带上"依赖判定 + 实测结论"两块
            val srcBody = gateway.httpText(ep.port, "/api/manga/sources").body
            val srcs = EngineData.sources(srcBody)
            assertTrue("漫画源列表不应为空", srcs.isNotEmpty())
            for (s in srcs) {
                assertTrue("实测状态必须在允许集合内：${s.key}=${s.verifyStatus}",
                    s.verifyStatus in allowed + "pending")
                if (s.verifyStatus == "verified") {
                    assertTrue("列表里标记实测通过的源必须带时间", s.verifyTestedAt.isNotBlank())
                }
            }
            ev("漫画源 ${srcs.size} 个，已实测通过 ${srcs.count { it.verifyStatus == "verified" }} 个")

            // 依赖不可用的源：必须标 unsupported 且写明原因（不偷偷换通道）
            val unsupported = srcs.filter { it.status == "unsupported" }
            for (s in unsupported) {
                assertTrue("依赖不可用的源必须给出原因：${s.key}", s.reason.isNotBlank())
            }
            if (unsupported.isNotEmpty()) {
                ev("依赖不可用 ${unsupported.size} 个，例：${unsupported[0].key} → ${unsupported[0].reason.take(50)}")
            }

            // 启动一轮（先只跑 1 个源），轮询到结束
            val start = gateway.httpPost(ep.port, "/api/manga/sources/verify",
                JSONObject().put("limit", 1).put("skip_verified_days", 0).toString())
            assertTrue("启动应被接受：HTTP ${start.code} ${start.body.take(80)}",
                start.code == 202 || start.code == 200)
            var finished = false
            var last = runCatching { JSONObject(start.body) }.getOrNull() ?: JSONObject()
            for (i in 0 until 150) {
                delay(3000)
                val o = runCatching {
                    JSONObject(gateway.httpText(ep.port, "/api/manga/sources/verify/status").body)
                }.getOrNull() ?: continue
                last = o
                if (o.optString("status") != "running") { finished = true; break }
            }
            assertTrue("验证未在预期时间内结束", finished)
            assertEquals("应以 done 结束：${last.optString("error")}", "done", last.optString("status"))
            ev("单源验证结束：done=${last.optInt("done")}/${last.optInt("total")}")

            // 结果与不变式
            val resBody = gateway.httpText(ep.port, "/api/manga/sources/verify/results").body
            val raw = JSONObject(resBody)
            val items = raw.optJSONArray("items")!!
            assertTrue("应有结果", items.length() > 0)
            var verified = 0
            for (i in 0 until items.length()) {
                val it = items.optJSONObject(i)!!
                val status = it.optString("status")
                assertTrue("非法状态：$status", status in allowed)
                assertTrue("每条都要有验证时间", it.optString("tested_at").isNotBlank())
                if (status == "verified") {
                    verified++
                    val st = it.optJSONObject("stages")!!
                    for (stage in listOf("search", "detail", "images")) {
                        val s = st.optJSONObject(stage)
                        assertTrue("verified 缺少 $stage 阶段", s != null && s.optBoolean("ok"))
                    }
                    assertTrue("verified 必须有搜索结果数",
                        st.optJSONObject("search")!!.optInt("count") > 0)
                    assertTrue("verified 必须有目录话数",
                        st.optJSONObject("detail")!!.optInt("count") > 0)
                    assertTrue("verified 必须有图片张数",
                        st.optJSONObject("images")!!.optInt("count") > 0)
                    assertTrue("verified 的首图字节必须大于 1000（真正能看）",
                        st.optJSONObject("images")!!.optInt("bytes") > 1000)
                } else {
                    assertTrue("非 verified 必须给出原因：${it.optString("key")}",
                        it.optString("reason").isNotBlank())
                }
            }
            // 汇总口径：分母是已注册源数，未实测的必须暴露
            val summary = raw.optJSONObject("summary")!!
            assertTrue("汇总应给出已注册源总数", summary.optInt("registered_total") >= 1)
            assertEquals("通过率应为 通过数/已注册数",
                Math.round(summary.optInt("verified") * 100.0 / summary.optInt("registered_total")).toInt(),
                summary.optInt("pass_rate"))
            ev("汇总：通过 ${summary.optInt("verified")}/${summary.optInt("registered_total")} " +
                "= ${summary.optInt("pass_rate")}%，未实测 ${summary.optInt("never_attempted")}，" +
                "本轮 verified=$verified")

            // 8) 漫画分类浏览：只有自己声明了分类的源才可用（MangaDex 已实现）
            val browse = gateway.httpText(ep.port, "/api/manga/browse?source=mangadex")
            assertTrue("MangaDex 浏览接口应有响应：HTTP ${browse.code}", browse.ok)
            val bo = runCatching { JSONObject(browse.body) }.getOrNull()!!
            assertTrue("应返回分类列表（key/name）", (bo.optJSONArray("categories")?.length() ?: 0) > 0)
            assertTrue("应返回条目数组", bo.has("results"))
            ev("漫画分类浏览：categories=${bo.optJSONArray("categories")?.length()} " +
                "条目=${bo.optJSONArray("results")?.length()} 首类=${bo.optString("category")}")
            // 未实现分类的源必须明确 404，且带原因（不编造榜单）
            val noBrowse = gateway.httpText(ep.port, "/api/manga/browse?source=copymanga")
            assertTrue("未提供分类的源不应成功", !noBrowse.ok)
            ev("未提供分类的源：HTTP ${noBrowse.code}")

            // 列表接口的 verified 必须与实测结论一致（不能一个说通过、一个说没测）
            val after = EngineData.sources(gateway.httpText(ep.port, "/api/manga/sources").body)
            for (s in after) {
                val fromResults = (0 until items.length()).any {
                    val o = items.optJSONObject(it)!!
                    o.optString("key") == s.key && o.optString("status") == "verified"
                }
                assertEquals("列表 verified 与实测结论不一致：${s.key}",
                    fromResults, s.verifyStatus == "verified")
            }
            ev("列表 verified 字段与实测结论一致")
        }
    }

    @Test
    fun mangaVerifyFullBatch() {
        assumeTrue("需要 -e mbatch 1 才跑全量漫画源批次",
            InstrumentationRegistry.getArguments().getString("mbatch") == "1")
        runBlocking {
            val state = gateway.connect()
            assertTrue("引擎未就绪：$state", state is EngineState.Ready)
            val ep = gateway.currentEndpoint()!!
            val registered = EngineData.sources(
                gateway.httpText(ep.port, "/api/manga/sources").body).size

            val start = gateway.httpPost(ep.port, "/api/manga/sources/verify",
                JSONObject().put("limit", 12).put("skip_verified_days", 0).toString())
            assertTrue("启动应被接受：HTTP ${start.code}", start.code == 202 || start.code == 200)
            var last = runCatching { JSONObject(start.body) }.getOrNull() ?: JSONObject()
            var finished = false
            for (i in 0 until 400) {
                delay(3000)
                val o = runCatching {
                    JSONObject(gateway.httpText(ep.port, "/api/manga/sources/verify/status").body)
                }.getOrNull() ?: continue
                last = o
                if (o.optString("status") != "running") { finished = true; break }
            }
            assertTrue("全量批次未在预期时间内结束", finished)
            assertEquals("应以 done 结束：${last.optString("error")}", "done", last.optString("status"))

            val raw = JSONObject(gateway.httpText(ep.port, "/api/manga/sources/verify/results").body)
            val items = raw.optJSONArray("items")!!
            for (i in 0 until items.length()) {
                val it = items.optJSONObject(i)!!
                val st = it.optJSONObject("stages") ?: JSONObject()
                val d = st.optJSONObject("detail"); val im = st.optJSONObject("images")
                ev("${it.optString("status").padEnd(11)} ${it.optString("name").take(18)}" +
                    (if (it.optString("status") == "verified")
                        " 搜${st.optJSONObject("search")?.optInt("count")}/目录${d?.optInt("count")}话/" +
                            "图${im?.optInt("count")}张/首图${im?.optInt("bytes")}字节"
                     else " " + it.optString("reason").take(46)))
            }
            val sm = raw.optJSONObject("summary")!!
            assertEquals("注册源数与列表一致", registered, sm.optInt("registered_total"))
            assertEquals("全量批次后不应仍有未实测的源", 0, sm.optInt("never_attempted"))
            ev("汇总：通过 ${sm.optInt("verified")}/${sm.optInt("registered_total")} " +
                "= ${sm.optInt("pass_rate")}%；部分 ${sm.optInt("partial")}，失败 ${sm.optInt("failed")}，" +
                "未支持 ${sm.optInt("unsupported")}")
        }
    }
}
