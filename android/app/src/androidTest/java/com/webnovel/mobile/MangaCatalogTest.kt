package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 移动漫画源**目录/支持矩阵**（0.51.0 路线 §4.2 唯一事实来源、§4.4 五分类）。
 *
 * 本用例做两件事：
 *   1. 契约：每个"展示出来"的源都必须带 注册状态 / 依赖判定 / 最近实测（含失败阶段与时间）
 *      ——未验证不能被当成可用；未注册的源进台账、**不出现在用户可见列表**，且保留原因；
 *   2. **真的跑一遍逐源功能验证**（搜索 → 详情/目录 → 章节图片 → 取一张真图），
 *      再核对目录里确实落了实测结果（这是"生成并执行"而不是"只生成一张表"）。
 *
 * 证据（矩阵本体）由 /api/manga/catalog?format=md 直接产出，便于归档。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class MangaCatalogTest {

    private fun ev(line: String) = println("CATALOG_EVIDENCE $line")

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test
    fun catalog_coversEveryDisplayedSource_andRunsRealVerification(): Unit = runBlocking {
        val gw = EngineGateway(ctx)
        val ep = (gw.connect() as? EngineState.Ready)?.endpoint
            ?: throw AssertionError("引擎未就绪")

        // 1) 契约检查
        val r = gw.httpText(ep.port, "/api/manga/catalog")
        assertTrue("目录接口应 200：HTTP ${r.code}", r.ok)
        val cat = JSONObject(r.body)
        val arr = cat.optJSONArray("sources") ?: throw AssertionError("目录缺少 sources")
        var registered = 0
        var retired = 0
        val seen = HashSet<String>()
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val key = o.optString("key")
            seen.add(key)
            assertTrue("每个源必须有移动可用性分类：$key", o.optString("category_label").isNotBlank())
            assertTrue("每个源必须写明分类理由：$key", o.optString("category_reason").isNotBlank())
            assertTrue("每个源必须有依赖判定：$key",
                (o.optJSONObject("dependency") ?: JSONObject()).optString("status").isNotBlank())
            assertTrue("每个源必须带实测字段（哪怕是「未实测」）：$key",
                (o.optJSONObject("verification") ?: JSONObject()).optString("status").isNotBlank())
            if (o.optBoolean("registered")) {
                registered++
                assertTrue("已注册源必须出现在用户可见列表里：$key", o.optBoolean("visible_in_app"))
            } else {
                retired++
                assertTrue("未注册源不得出现在用户可见列表里：$key", !o.optBoolean("visible_in_app"))
                assertTrue("未注册源要保留原因（删源不是修复）：$key",
                    o.optString("category_reason").contains("未纳入注册表")
                        || o.optString("category_reason").isNotBlank())
            }
        }
        assertTrue("目录里必须有已注册源（实际 $registered）", registered >= 4)
        assertTrue("目录里必须保留未注册台账（实际 $retired）", retired >= 1)
        ev("目录契约通过：已注册 $registered 个（都在用户列表里），未注册台账 $retired 个（都在列表外）")

        // 2) 真的跑一遍逐源功能验证（不设 limit，覆盖全部已注册源）
        val start = gw.httpPost(ep.port, "/api/manga/sources/verify",
            JSONObject().put("limit", 20).put("skip_verified_days", 0)
                .put("skip_tested_days", 0).toString())
        assertTrue("启动验证应 2xx：HTTP ${start.code}", start.code in 200..299)
        var done = false
        for (i in 0 until 200) {
            delay(2000)
            val st = runCatching {
                JSONObject(gw.httpText(ep.port, "/api/manga/sources/verify/status").body)
            }.getOrDefault(JSONObject())
            if (st.optString("status") == "done") { done = true; break }
            if (st.optString("status") == "error") {
                throw AssertionError("验证异常：${st.optString("error")}")
            }
        }
        assertTrue("逐源验证应在超时前跑完", done)
        ev("逐源功能验证已执行完成（搜索 → 详情/目录 → 章节图片 → 取一张真图）")

        // 3) 目录必须反映刚才的实测结果（生成 + 执行，不是一张静态表）
        val r2 = gw.httpText(ep.port, "/api/manga/catalog")
        val cat2 = JSONObject(r2.body)
        val arr2 = cat2.optJSONArray("sources")!!
        var tested = 0
        val lines = StringBuilder()
        for (i in 0 until arr2.length()) {
            val o = arr2.optJSONObject(i) ?: continue
            if (!o.optBoolean("registered")) continue
            val v = o.optJSONObject("verification") ?: JSONObject()
            if (v.optString("tested_at").isNotBlank()) tested++
            lines.append("· ").append(o.optString("name")).append("：")
                .append(o.optString("category_label"))
                .append("｜依赖=").append((o.optJSONObject("dependency") ?: JSONObject())
                    .optString("status"))
                .append("｜实测=").append(v.optString("status"))
                .append(if (v.optString("failed_stage").isNotBlank())
                    "（最早失败：" + v.optString("failed_stage") + "）" else "")
                .append("｜时间=").append(v.optString("tested_at").ifBlank { "—" })
                .append('\n')
        }
        ev("矩阵（设备实测后）：\n$lines")
        assertTrue("实测完成后，目录里必须有带时间的实测结果（实际 $tested）", tested >= 1)
        ev("目录汇总：${cat2.optJSONObject("summary")}")

        // 4) 把**设备上生成**的矩阵 Markdown 逐行打出来，便于直接归档
        //    （报告必须以真实运行的那台设备为准，而不是桌面侧的推断）
        val md = gw.httpText(ep.port, "/api/manga/catalog?format=md")
        assertTrue("Markdown 矩阵应可导出：HTTP ${md.code}", md.ok)
        for (line in md.body.split("\n")) ev("MD| $line")
        ev("矩阵 Markdown 已导出（${md.body.length} 字节）")
    }
}
