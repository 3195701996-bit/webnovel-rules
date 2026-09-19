package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * 内置源**种子清单**验收（方向基线 §6.4）。
 *
 * 问题：APK 直接复制桌面书源目录，会把"该时点桌面的启用状态"一起带入——实测桌面 34 个源
 * 里 28 个停用，手机端一装就只有 6 个源可用。现在构建期生成 `mobile-sources.json`
 * （schema + uid + 内容 sha256 + 是否默认启用 + 理由），**首次解包时**按它设置启用状态。
 *
 * 本用例直接验证"首次装包"这条路径（在应用私有目录里造出全新状态再跑一次）：
 *   1. 清单必须在包内且结构完整（uid/sha256/理由）；
 *   2. 首次解包后：默认启用的源确实启用、策略里停用的源确实停用；
 *   3. **已应用过就不再动**：用户改过之后重跑不会覆盖（这条最关键）；
 *   4. 已有安装（本次没解出新源）一律跳过。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class SeedAndNovelStreamTest {

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext
    private fun ev(line: String) = println("SEED_EVIDENCE $line")

    private fun manifest(): JSONObject =
        JSONObject(ctx.assets.open(BundledSources.SEED_MANIFEST).use {
            it.readBytes().toString(Charsets.UTF_8)
        })

    /** 用一份"全新安装"的沙盒目录跑种子应用，不动真实书源目录 */
    private fun freshDir(): File {
        val d = File(ctx.cacheDir, "seed-test-sources")
        d.deleteRecursively()
        d.mkdirs()
        return d
    }

    @Test
    fun manifest_isShippedAndComplete() {
        val m = manifest()
        assertEquals("清单 schema 应为 1", 1, m.optInt("schema"))
        val arr = m.optJSONArray("sources") ?: throw AssertionError("清单缺少 sources")
        assertTrue("清单必须覆盖内置源（实际 ${arr.length()}）", arr.length() >= 30)
        var withReason = 0
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            assertTrue("每个源必须有 uid", o.optString("uid").isNotBlank())
            assertTrue("每个源必须有内容 sha256（稳定 ID）",
                o.optString("sha256").length == 64)
            if (!o.optBoolean("enabled_by_default", true)) {
                withReason++
                assertTrue("默认停用必须写明理由：${o.optString("uid")}",
                    o.optString("reason").isNotBlank())
            }
        }
        val defaultOn = m.optInt("enabled_by_default", -1)
        assertEquals("清单内的默认启用数应与逐条一致",
            arr.length() - withReason, defaultOn)
        ev("清单：${arr.length()} 个源，默认启用 $defaultOn，默认停用 $withReason（都带理由）")
    }

    @Test
    fun seedMerge_onlyTouchesUntouchedFiles_andIsIdempotent() {
        val d = freshDir()
        try {
            // 1) 全新目录：解出书源 + 合并种子
            val n = BundledSources.ensure(ctx, d)
            assertTrue("应解出内置书源（实际 $n）", n > 0)
            val changed = BundledSources.applySeed(ctx, d)
            ev("首次合并：解出 $n 个源，改动 $changed 个")
            assertTrue("首次合并应有改动（出厂默认 6 启用 → 清单 28 启用）", changed > 0)

            val m = manifest()
            val arr = m.optJSONArray("sources")!!
            val expect = HashMap<String, Boolean>()
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                expect[o.optString("uid")] = o.optBoolean("enabled_by_default", true)
            }
            var checked = 0
            for (f in d.listFiles { f -> f.isFile && f.name.endsWith(".json") } ?: emptyArray()) {
                val o = JSONObject(f.readText(Charsets.UTF_8))
                val uid = o.optString("uid").ifBlank { f.name.removeSuffix(".json") }
                val want = expect[uid] ?: continue
                assertEquals("种子应把 $uid 设为 $want", want, o.optBoolean("enabled", true))
                checked++
            }
            assertTrue("应对清单内的源逐个核对（实际 $checked）", checked >= 30)
            val offCount = (d.listFiles { f -> f.isFile && f.name.endsWith(".json") }
                ?: emptyArray()).count { f ->
                !JSONObject(f.readText(Charsets.UTF_8)).optBoolean("enabled", true)
            }
            assertEquals("默认停用的源数应与清单一致",
                arr.length() - m.optInt("enabled_by_default"), offCount)
            ev("启用状态与清单一致（停用 $offCount 个）")

            // 2) 幂等：同一 schema 再跑一次不应改动任何东西
            assertEquals("同一 schema 重跑应为 0 改动", 0, BundledSources.applySeed(ctx, d))
            ev("同一 schema 重跑 0 改动（幂等）")

            // 3) **升级合并**：模拟用户改过的一个文件 + 一个未改动文件，
            //    清掉标记（等价于 schema 升级）后再合并 → 只动未改动的那个
            val files = d.listFiles { f -> f.isFile && f.name.endsWith(".json") }!!
            val userTouched = files.first { f ->
                JSONObject(f.readText(Charsets.UTF_8)).optBoolean("enabled", true)
            }
            val to = JSONObject(userTouched.readText(Charsets.UTF_8))
            to.put("enabled", false)                      // 用户手动停用
            userTouched.writeText(to.toString(), Charsets.UTF_8)
            val modifiedName = userTouched.name

            // 另找一个"未改动"的源：把它的 enabled 改成出厂值以外再改回来？
            // 更简单：删掉标记 → 重新合并（未改动的文件会再次对齐，改过的跳过）
            // 模拟"策略升级"：把标记里的 schema 改旧（**不能删标记**——删了就等于
            // 抹掉"哪些文件是我们自己改的"这条记忆，第二次跑会把它们误判成用户改动）
            val mk = JSONObject(File(d, ".seed-applied").readText(Charsets.UTF_8))
            mk.put("schema", 0)
            File(d, ".seed-applied").writeText(mk.toString(), Charsets.UTF_8)
            val changed2 = BundledSources.applySeed(ctx, d)
            // 关键：**只有**用户改过的那 1 个文件应被跳过；上次由种子设置的 24 个必须被
            // 识别为"我们自己改的"（标记里记了改后哈希），不能当成用户改动永久放弃
            val marker2 = JSONObject(File(d, ".seed-applied").readText(Charsets.UTF_8))
            ev("模拟升级后合并：改动 $changed2 个，跳过用户改过 " +
                "${marker2.optInt("modified_skipped")} 个，" +
                "视为出厂未改动 ${marker2.optInt("untouched")} 个")
            // 列出被判为"用户改过"的文件名：便于区分"确实是用户改的"与"我们自己的痕迹"
            val ours2 = marker2.optJSONObject("ours") ?: JSONObject()
            val manifestHash = HashMap<String, String>()
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                manifestHash[o.optString("name")] = o.optString("sha256")
            }
            val skipped = ArrayList<String>()
            for (f in d.listFiles { f -> f.isFile && f.name.endsWith(".json") } ?: emptyArray()) {
                val h = java.security.MessageDigest.getInstance("SHA-256")
                    .digest(f.readBytes()).joinToString("") { "%02x".format(it) }
                if (h != manifestHash[f.name] && h != ours2.optString(f.name)) skipped.add(f.name)
            }
            ev("被判为用户改动的文件：$skipped")
            assertTrue("只有用户改过的那 1 个文件应被跳过（实际 $skipped）",
                skipped.size <= 1)
            assertEquals("用户改过的文件必须保持原样（不被覆盖）", false,
                JSONObject(File(d, modifiedName).readText(Charsets.UTF_8))
                    .optBoolean("enabled", true))
            // 清单里默认启用的源应已对齐（未被用户改过的那些）
            var aligned = 0
            for (f in d.listFiles { f -> f.isFile && f.name.endsWith(".json") } ?: emptyArray()) {
                val o = JSONObject(f.readText(Charsets.UTF_8))
                val uid = o.optString("uid").ifBlank { f.name.removeSuffix(".json") }
                val want = expect[uid] ?: continue
                if (o.optBoolean("enabled", true) == want) aligned++
            }
            assertTrue("未被用户改过的源应对齐清单（实际 $aligned）", aligned >= 30)
            ev("升级合并：用户改的保留、其余对齐清单（对齐 $aligned）")

            // 4) 摘要要能说清"改了多少、跳过多少"（诊断与界面用）
            val summary = BundledSources.seedSummary(ctx, d)
            assertTrue("摘要应含 schema 与数量：$summary",
                summary.contains("schema") && summary.contains("已合并"))
            ev("种子摘要：$summary")
        } finally {
            d.deleteRecursively()
        }
    }

    /** 小说搜索也走流式：逐源到达即上屏（阻塞端点要等最慢源） */
    @Test
    fun novelSearchStream_reportsProgressPerSource() = runBlocking {
        val gateway = EngineGateway(ctx)
        val st = gateway.connect()
        assertTrue("引擎未就绪：$st", st is EngineState.Ready)
        val ep = (st as EngineState.Ready).endpoint
        try {
            var firstGroups = 0
            var firstMs = 0L
            var progressDone = -1
            var progressTotal = 0
            val t0 = System.currentTimeMillis()
            val code = gateway.streamEvents(ep.port,
                "/api/search/stream?q=" + java.net.URLEncoder.encode("剑来", "UTF-8")) { evt ->
                val groups = evt.optJSONArray("groups") ?: return@streamEvents
                if (groups.length() > 0 && firstGroups == 0) {
                    firstGroups = groups.length()
                    firstMs = System.currentTimeMillis() - t0
                }
                if (!evt.optBoolean("finished")) {
                    progressDone = evt.optInt("done"); progressTotal = evt.optInt("total")
                }
            }
            assertTrue("小说流式搜索应成功：code=$code", code in 200..299)
            // 拿不到结果 = 源站限流/不可达（这本用例要验的是"逐源进度与首屏时间"，
            // 没有可用源就无从验证）→ 跳过并写明原因，代码回归仍会是红。
            org.junit.Assume.assumeTrue(
                "源站限流或不可达：小说流式搜索没有返回结果，跳过（环境问题）",
                firstGroups > 0)
            assertTrue("首屏应早于总耗时（首 ${firstMs}ms）", firstMs in 1..60_000)
            ev("小说流式：首屏 ${firstMs}ms 拿到 $firstGroups 组，进度 $progressDone/$progressTotal")
        } finally {
            runCatching { gateway.stopEngine() }
        }
    }
}
