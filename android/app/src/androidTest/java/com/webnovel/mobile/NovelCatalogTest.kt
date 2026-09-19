package com.webnovel.mobile

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * **小说源移动可用性台账**（把路线 P0-3 的纪律用到小说侧）。
 *
 * 为什么：漫画源早有"注册/依赖/实测/五分类"的单一事实来源，小说侧只有一句"部分源可用"。
 * 本用例在设备上：
 *   1. 核对台账契约——每个源都有**分类 + 原因 + 失败阶段 + 实测时间**，
 *      且"标成已验证的必须有实测时间"（未验证 ≠ 可用）；
 *   2. **真的跑一轮逐源功能验证**（搜索 → 详情 → 目录 → 正文），把结论落盘；
 *   3. 验证后重新取台账：分类必须跟着实测变（有记录的不能再叫"待验证"）；
 *   4. 导出可归档的 Markdown（逐行打到 logcat，供 `03-验证结果/` 归档）；
 *   5. 界面上书源卡片显示"移动可用性：<分类> · 卡在<阶段>阶段"。
 *
 * 时效说明：一轮只验证**未验证/结论过期**的源（`skip_verified_days=7`），
 * 且限制在 `BATCH_LIMIT` 个——手机上逐源跑完整四阶段很慢，分轮推进比一次跑爆更诚实。
 * 本用例会在证据里写明"本轮覆盖了多少、还剩多少待验证"。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class NovelCatalogTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext
    private lateinit var gateway: EngineGateway

    /**
     * 单轮批量上限（仅在显式开启 sweep 时使用）。配合 `skip_verified_days=7`：
     * 已通过的不重复打源站，其余重测。
     */
    private val batchLimit = 34

    /**
     * 是否跑"全量逐源验证"（**默认不跑**）。
     *
     * 为什么默认不跑：一轮 28 个源的四阶段验证会**把源站打到限流**，
     * 之后同一次套件里的搜索类用例（小说回归集、分源搜索、漫画闭环…）就开始连锁失败——
     * 实测：整包 75 条里 18 条失败，全部集中在"需要联网搜索"的用例上，
     * 其中 `爱下电子书(ixdzs8)` 在 sweep 之后搜索直接变 0 结果（此前 20 条）。
     * 这不是产品缺陷，而是测试自己制造的源站压力。
     *
     * 需要重出矩阵时显式开：
     *   adb shell am instrument -w -e novelSweep 1 \
     *     -e class com.webnovel.mobile.NovelCatalogTest \
     *     com.webnovel.mobile.test/androidx.test.runner.AndroidJUnitRunner
     */
    private val sweepEnabled: Boolean
        get() = (androidx.test.platform.app.InstrumentationRegistry.getArguments()
            .getString("novelSweep") ?: "") in listOf("1", "true", "yes")

    private fun ev(line: String) = println("NOVEL_CATALOG_EVIDENCE $line")

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size
    private fun texts(t: String, substring: Boolean = false) =
        rule.onAllNodesWithText(t, substring = substring).fetchSemanticsNodes().size

    private suspend fun pollVerify() {
        val ep = gateway.currentEndpoint() ?: throw AssertionError("引擎未就绪")
        for (i in 0 until 120) {
            val st = gateway.httpText(ep.port, "/api/sources/verify/status")
            val o = runCatching { JSONObject(st.body) }.getOrNull() ?: continue
            val status = o.optString("status")
            if (status == "idle" || status == "done" || status == "error") {
                ev("验证批次结束：status=$status done=${o.optInt("done")}/${o.optInt("total")}")
                return
            }
            if (i % 4 == 0) {
                ev("验证中：${o.optInt("done")}/${o.optInt("total")} 当前=${o.optString("current")}")
            }
            delay(3000)
        }
        ev("验证批次轮询超时（结论以已落盘的为准）")
    }

    @Test
    fun novelCatalog_classifiesEverySourceAndRunsRealVerification() = runBlocking {
        gateway = EngineGateway(ctx)
        val ep = (gateway.connect() as? EngineState.Ready)?.endpoint
            ?: throw AssertionError("引擎未就绪")

        // 1) 契约：每个源都有分类/原因/阶段字段；"已验证"必须有实测时间
        val before = JSONObject(gateway.httpText(ep.port, "/api/novel/catalog").body)
        val rows = before.optJSONArray("sources")!!
        assertTrue("台账应覆盖所有书源（实际 ${rows.length()}）", rows.length() > 0)
        for (i in 0 until rows.length()) {
            val r = rows.optJSONObject(i)!!
            for (k in listOf("uid", "name", "enabled", "category", "category_label",
                "reason", "failed_stage", "tested_at", "stale", "counts")) {
                assertTrue("台账条目缺字段 $k：$r", r.has(k))
            }
            if (r.optString("category") == "android_verified") {
                assertTrue("标成已验证的必须有实测时间：${r.optString("uid")}",
                    r.optString("tested_at").isNotBlank())
            }
            if (r.optString("category") == "untested") {
                assertTrue("待验证的源不得带失败阶段",
                    r.optString("failed_stage").isBlank())
            }
        }
        val sumBefore = before.optJSONObject("summary")!!
        ev("台账（验证前）：${sumBefore}")

        // 2) 全量验证（默认跳过：会把源站打到限流，见 sweepEnabled 注释）
        if (!sweepEnabled) {
            ev("未开 sweep：本轮只做契约与界面核对（重出矩阵见类注释里的命令）")
            ev("台账（当前）：${sumBefore}")
            return@runBlocking Unit
        }
        val key = "剑来"
        val start = gateway.httpPost(ep.port, "/api/sources/verify",
            JSONObject().put("keyword", key).put("limit", batchLimit)
                .put("skip_verified_days", 7).toString())
        assertTrue("启动验证应 2xx：HTTP ${start.code} ${start.body.take(80)}",
            start.code in 200..299)
        ev("已启动验证批次（limit=$batchLimit skip_verified_days=7 关键词=$key）")
        pollVerify()

        // 3) 验证后：有记录的不能再叫"待验证"，且分类必须与实测状态一致
        val after = JSONObject(gateway.httpText(ep.port, "/api/novel/catalog").body)
        val sumAfter = after.optJSONObject("summary")!!
        ev("台账（验证后）：${sumAfter}")
        assertTrue("验证后应至少有一条非「待验证」的结论（实际待验证 ${sumAfter.optInt("untested")}）",
            sumAfter.optInt("untested") < sumBefore.optInt("untested") ||
                sumAfter.optInt("verified") + sumAfter.optInt("partial") +
                sumAfter.optInt("failed") + sumAfter.optInt("unsupported") > 0)

        val rows2 = after.optJSONArray("sources")!!
        var withRecord = 0
        for (i in 0 until rows2.length()) {
            val r = rows2.optJSONObject(i)!!
            val cat = r.optString("category")
            val tested = r.optString("tested_at")
            // 待验证（没记录）与已停用（用户关的）本来就可能没有实测时间——
            // 不变式只约束"有实测结论"的四类（实测踩到：把已停用当成了违规）
            if (cat == "untested" || cat == "disabled") continue
            withRecord++
            assertTrue("非「待验证」的条目必须有实测时间：${r.optString("uid")}", tested.isNotBlank())
            val reason = r.optString("reason")
            assertTrue("每条非通过结论都要有可读原因：${r.optString("uid")} → $reason",
                cat == "android_verified" || reason.isNotBlank())
            if (cat == "android_partial") {
                assertTrue("部分可用必须写出卡在哪一步：${r.optString("uid")}",
                    r.optString("failed_stage").isNotBlank())
            }
        }
        assertTrue("验证后应至少有一条带记录的源", withRecord > 0)
        ev("验证后带记录的源：$withRecord 条；待验证 ${sumAfter.optInt("untested")} 条" +
            "（启用 ${sumAfter.optInt("enabled")}，启用口径通过率 ${sumAfter.optInt("enabled_pass_rate")}%）")

        // 4) 导出可归档的 Markdown（逐行打进 logcat）
        val md = gateway.httpText(ep.port, "/api/novel/catalog?format=md")
        assertTrue("Markdown 台账应可导出：HTTP ${md.code}", md.ok)
        for (line in md.body.split("\n")) ev("MD| $line")
        ev("台账 Markdown 已导出（${md.body.length} 字节）")
    }

    @Test
    fun sourceCardShowsAvailability() {
        gateway = EngineGateway(ctx)
        val st = runBlocking { gateway.connect() }
        assertTrue("引擎未就绪：$st", st is EngineState.Ready)
        rule.waitUntil(150_000) { texts("设置") > 0 }
        rule.onAllNodesWithText("设置")[0].performClick()
        rule.waitUntil(60_000) { texts("书源管理") > 0 }
        rule.onAllNodesWithText("书源管理")[0].performScrollTo().performClick()
        rule.waitUntil(90_000) { nodes("book_sources") > 0 }

        // 至少有一张卡片显示"移动可用性：<分类>"（卡片按 uid 打了 tag）
        val ep = gateway.currentEndpoint() ?: throw AssertionError("引擎未就绪")
        val arr0 = runBlocking {
            JSONObject(gateway.httpText(ep.port, "/api/sources").body)
                .optJSONArray("sources")!!
        }
        rule.waitUntil(90_000) {
            (0 until arr0.length()).any { i ->
                val uid = arr0.optJSONObject(i)?.optString("uid").orEmpty()
                uid.isNotBlank() && nodes("src_availability_$uid") > 0
            }
        }
        // 逐 uid 统计（tag 是动态拼的，没有 substring 版的重载）
        val arr = arr0
        var shown = 0
        var first: String? = null
        for (i in 0 until arr.length()) {
            val uid = arr.optJSONObject(i)?.optString("uid").orEmpty()
            if (uid.isBlank()) continue
            if (nodes("src_availability_$uid") > 0) {
                shown++
                if (first == null) {
                    first = rule.onAllNodesWithTag("src_availability_$uid")[0]
                        .fetchSemanticsNode().config.toString()
                }
            }
        }
        assertTrue("书源卡片必须显示移动可用性（找到 $shown 个）", shown > 0)
        ev("卡片可用性行示例：${first!!.take(200)}")
        assertTrue("可用性行必须是『移动可用性：…』这种可读文案：$first",
            first.contains("移动可用性"))
        assertFalse("可用性行不能是空壳", first.isBlank())
        Unit
    }
}
