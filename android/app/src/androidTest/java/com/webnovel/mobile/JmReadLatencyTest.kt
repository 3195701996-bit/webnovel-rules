package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * **jm（禁漫）阅读延迟剖析**——用户反馈"只有 mangadex 加载快，禁漫加载过慢"。
 *
 * 这个用例不改代码，只**测时间并留证据**，把"慢"拆成可归因的几段：
 *   T1 章节图列表（/chapter/<id>，含源站 /chapter?id= 往返）
 *   T2 首图冷启动（/img/0，含可能的图片域探测）
 *   T3 随后 5 张**串行**取图（逐张平均）
 *   T4 随后 6 张**并行**取图（阅读器实际是并行的，看并行度是否真的生效）
 *   T5 同章**再取一次**（本地缓存直出应有的速度）
 *   T6 换一话重复 T1/T2（判断"每话都要重新探测/重新拿列表"）
 * 并把每段的毫秒数打进 logcat（`JM_LATENCY_EVIDENCE`），供归档与优化前后对比。
 *
 * 只读：不下载、不改缓存、不写任务。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class JmReadLatencyTest {

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext

    private fun ev(line: String) = println("JM_LATENCY_EVIDENCE $line")

    private fun now() = System.nanoTime()

    private fun ms(t0: Long) = (System.nanoTime() - t0) / 1_000_000

    private fun runBlockingDelay(ms: Long) = runBlocking { kotlinx.coroutines.delay(ms) }

    @Test
    fun profileJmChapterReadLatency() = runBlocking {
        val gw = EngineGateway(ctx)
        val ep = (gw.connect() as? EngineState.Ready)?.endpoint
            ?: throw AssertionError("引擎未就绪")

        // 直接解析一部 **jm** 作品：不能用 MangaFixture.pick——它命中缓存时会给回
        // mangadex 的夹具（实测踩到：整轮剖析测的其实是 mangadex，数字毫无参考价值）。
        val src = "jm"
        var cid = ""
        var ch = ""
        var title = ""
        val kws = listOf("巨人", "海贼王", "火影忍者")
        // 用**排行/分类浏览**解析 jm 作品：jm 的搜索在本机网络下常返回 0 条
        // （实测 3 关键词 × 3 轮全 0），而 browse 稳定可用（漫画浏览页就走它）。
        outer@ for (round in 0 until 3) {
            val t0 = now()
            val r = gw.httpText(ep.port, "/api/manga/browse?source=jm&page=1")
            val dt = ms(t0)
            if (!r.ok) { ev("jm 浏览 HTTP ${r.code}（${dt}ms）"); kotlinx.coroutines.delay(3000L * (round + 1)); continue }
            val arr = runCatching { JSONObject(r.body).optJSONArray("results") }.getOrNull()
            val first = arr?.optJSONObject(0)
            if (first == null) {
                ev("jm 浏览 0 条（${dt}ms）")
                kotlinx.coroutines.delay(3000L * (round + 1))
                continue
            }
            cid = first.optString("id")
            title = first.optString("title")
            ev("jm 浏览（排行） ${dt}ms → ${title.take(22)}（id=$cid）")
            if (cid.isNotBlank()) break@outer
        }
        assertTrue("应能从 jm 排行拿到作品（源站不可达时本用例失败，非 App 问题）",
            cid.isNotBlank())
        run {
            val d0 = now()
            val dr = gw.httpText(ep.port, "/api/manga/$src/$cid")
            ev("jm 详情 ${ms(d0)}ms HTTP ${dr.code}")
            val chapters = runCatching {
                JSONObject(dr.body).optJSONArray("chapters")
            }.getOrNull()
            assertTrue("jm 详情应有章节：${dr.body.take(160)}", (chapters?.length() ?: 0) > 0)
            ch = chapters!!.optJSONObject(0)!!.optString("id")
        }
        ev("目标：$src/$cid《$title》第 $ch 话")

        suspend fun timeIt(label: String, path: String): Pair<Long, String> {
            val t0 = now()
            val r = gw.httpText(ep.port, path)
            val dt = ms(t0)
            ev("$label  ${dt}ms  HTTP ${r.code}  ${r.body.length}B")
            return dt to r.body
        }

        // T1 章节图列表
        val (t1, body) = timeIt("T1 章节图列表", "/api/manga/$src/$cid/chapter/$ch")
        assertTrue("图列表应 200", body.isNotBlank())
        val o = JSONObject(body)
        val count = o.optInt("count")
        ev("T1 结果：count=$count local=${o.optBoolean("local")}")
        assertTrue("应有图片：$body", count > 0)

        // T2 首图（冷启动：可能触发图片域探测）
        val (t2, _) = timeIt("T2 首图(冷)", "/api/manga/$src/$cid/chapter/$ch/img/0")

        // T3 随后 5 张串行
        var serial = 0L
        val serialN = minOf(5, count - 1)
        for (i in 1..serialN) {
            val (d, _) = timeIt("T3 串行 #$i", "/api/manga/$src/$cid/chapter/$ch/img/$i")
            serial += d
        }

        // T4 并行 6 张（阅读器真实并发形态）
        val parFrom = serialN + 1
        val parTo = minOf(parFrom + 5, count - 1)
        var parTotal = 0L
        if (parTo >= parFrom) {
            val t0 = now()
            coroutineScope {
                (parFrom..parTo).map { i ->
                    async { gw.httpText(ep.port, "/api/manga/$src/$cid/chapter/$ch/img/$i") }
                }.forEach { it.await() }
            }
            parTotal = ms(t0)
            ev("T4 并行 ${parTo - parFrom + 1} 张  总 ${parTotal}ms  " +
                "平均 ${parTotal / (parTo - parFrom + 1)}ms")
        }

        // T5 同章再取一次（缓存直出）
        val (t5, _) = timeIt("T5 首图(再取)", "/api/manga/$src/$cid/chapter/$ch/img/0")

        // T6 预热效果：打开章节（T1 已经调用过 /chapter → 服务端会后台预热本章）
        //    等 3 秒后再连取 8 张：命中预热的应当是毫秒级（"加载过慢"的直接对照）
        runBlockingDelay(3000)
        var warmHit = 0
        var warmTotal = 0L
        val probeN = minOf(8, count)
        for (i in 0 until probeN) {
            val t0 = now()
            val r = gw.httpText(ep.port, "/api/manga/$src/$cid/chapter/$ch/img/$i")
            val dt = ms(t0)
            warmTotal += dt
            if (dt < 300) warmHit++
            if (!r.ok && r.code != 409) ev("T6 #$i HTTP ${r.code}（${dt}ms）")
        }
        ev("T6 预热效果：打开章节 3s 后连取 $probeN 张，共 ${warmTotal}ms；" +
            "其中 ${warmHit} 张为缓存直出（<300ms）")

        ev("==== 汇总 ====")
        ev("T1 图列表 ${t1}ms；T2 首图 ${t2}ms；T3 串行 ${serialN} 张共 ${serial}ms" +
            "（平均 ${if (serialN > 0) serial / serialN else 0}ms）" +
            "；T4 并行 ${parTo - parFrom + 1} 张共 ${parTotal}ms；T5 再取首图 ${t5}ms")
        assertTrue("图列表阶段应当有结果", count > 0)
        Unit
    }
}
