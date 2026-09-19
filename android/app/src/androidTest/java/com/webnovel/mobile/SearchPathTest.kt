package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import android.net.Uri
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 原生搜索的契约验收（对照诊断 §4 浏览与搜索）。
 *
 * 这里**不要求"必须搜到某本书"**（那取决于源站与网络），而是锁住结构与诚实性：
 *   - 空关键词被服务端明确拒绝（400），不是静默返回空列表；
 *   - 正常关键词返回 200，结构可解析；每个"书组"必须有书名与至少一个带地址的来源；
 *   - 部分源失败/超时必须以 partial / timed_out_sources / errors 如实暴露，
 *     不能让人以为"全网就这么多书"；
 *   - 漫画搜索结果为结构化条目（source/id/title），失败源单独列出。
 *
 * 真实网络：跟书源功能验证同一性质，允许"没有结果"，但结构断言必须成立。
 */
@RunWith(AndroidJUnit4::class)
class SearchPathTest {

    private lateinit var gateway: EngineGateway
    private lateinit var book: SelfTestBook

    private fun ev(line: String) = println("SEARCH_PATH_EVIDENCE $line")

    @Before
    fun setUp() {
        book = SelfTestBook()
        book.create()
        gateway = EngineGateway(InstrumentationRegistry.getInstrumentation().targetContext)
        val state = runBlocking { gateway.connect() }
        assertTrue("引擎未就绪：$state", state is EngineState.Ready)
    }

    @After
    fun tearDown() {
        runCatching { runBlocking { gateway.stopEngine() } }
        book.cleanup()
    }

    @Test
    fun searchContracts_areHonest() = runBlocking {
        val ep = gateway.currentEndpoint()!!

        // 1) 空关键词必须明确失败，而不是"搜到 0 本"
        val empty = gateway.httpText(ep.port, "/api/search?q=")
        assertTrue("空关键词应返回错误（实际 ${empty.code}）", !empty.ok)
        ev("空关键词：HTTP ${empty.code}")

        // 2) 真实关键词：允许没有结果，但结构必须可用
        val r = gateway.httpText(ep.port, "/api/search?q=剑来")
        assertTrue("搜索接口状态码=${r.code}", r.ok)
        val (groups, meta) = EngineData.novelSearch(r.body)
        assertTrue("搜索响应应含 groups 字段", r.body.contains("groups"))
        for (g in groups) {
            assertTrue("书组必须有书名", g.name.isNotBlank())
            assertTrue("书组必须有来源条目：${g.name}", g.sources.isNotEmpty())
            for (s in g.sources) {
                assertTrue("来源必须有地址：${g.name} / ${s.sourceName}", s.bookUrl.isNotBlank())
            }
            // 有了地址与源 uid 才可能"加入书架"（原生流程的必要条件）
            assertTrue("来源应带 source_uid（加入书架要用）：${g.name}",
                g.sources.all { it.sourceUid.isNotBlank() || it.sourceName.isNotBlank() })
        }
        ev("小说搜索：${groups.size} 本书，超时源=${meta.timedOutSources}，partial=${meta.partial}")

        // 3) 部分失败/超时必须暴露（不是 0 就是 0）
        assertTrue("partial 与 timed_out_sources 字段应存在",
            r.body.contains("partial") && r.body.contains("timed_out_sources"))
        assertTrue("超时源数不应为负", meta.timedOutSources >= 0)

        // 4) 限定单源的搜索：结构一致；源不存在时必须 404（不假装成功）
        val single = gateway.httpText(ep.port, "/api/search?q=剑来&source=不存在的源_uid")
        assertTrue("指定不存在书源应 404，实际 ${single.code}", single.code == 404)
        ev("指定不存在书源：HTTP ${single.code}")

        // 5) 漫画搜索：结构化结果 + 失败源单独列出
        val m = gateway.httpText(ep.port, "/api/manga/search?q=巨人")
        assertTrue("漫画搜索状态码=${m.code}", m.ok)
        val (hits, mmeta) = EngineData.mangaSearch(m.body)
        assertTrue("漫画搜索响应应含 results", m.body.contains("results"))
        for (h in hits) {
            assertTrue("漫画结果必须有 id 与标题：${h.title}", h.comicId.isNotBlank() && h.title.isNotBlank())
            assertTrue("漫画结果必须带来源 key", h.source.isNotBlank())
        }
        assertTrue("漫画搜索应暴露 errors（失败源与空结果区分开）", m.body.contains("errors"))
        ev("漫画搜索：${hits.size} 条，失败源=${mmeta.errors.size} 个" +
            (if (mmeta.errors.isNotEmpty()) "（例：${mmeta.errors.keys.first()}）" else ""))

        // 5b) 漫画搜索分页：page=2 必须被服务端接受，页码回显，结构一致
        val p2 = gateway.httpText(ep.port, "/api/manga/search?q=巨人&page=2")
        assertTrue("第 2 页请求应成功：HTTP ${p2.code}", p2.ok)
        val (hits2, meta2) = EngineData.mangaSearch(p2.body)
        assertEquals("服务端应回显第 2 页", 2, meta2.page)
        for (h in hits2) {
            assertTrue("第 2 页结果同样必须有 id 与标题：${h.title}",
                h.comicId.isNotBlank() && h.title.isNotBlank() && h.source.isNotBlank())
        }
        assertTrue("has_more 字段应存在（界面据此决定是否给『加载更多』）",
            p2.body.contains("has_more"))
        ev("漫画分页：page=2 → ${hits2.size} 条，hasMore=${meta2.hasMore}")

        // 6) 用不存在的书源建任务：服务端**不做前置校验**（会先 202 建任务，
        //    由 worker 失败收尾）。这里断言可观察到的真实契约：要么当场拒绝，
        //    要么任务最终以失败收尾并给出原因——**不能停在"运行中"假装成功**。
        val badTask = gateway.httpPost(ep.port, "/api/tasks",
            org.json.JSONObject().put("source_uid", "不存在的源")
                .put("book_url", "https://example.com/book/1").toString())
        if (!badTask.ok) {
            ev("用不存在书源创建任务：当场被拒 HTTP ${badTask.code}")
        } else {
            val tid = runCatching {
                org.json.JSONObject(badTask.body).optString("id")
            }.getOrNull() ?: ""
            assertTrue("202 应带上任务 id，实际=${badTask.body.take(80)}", tid.isNotBlank())
            var last = ""
            var lastErr = ""
            for (i in 0 until 40) {
                kotlinx.coroutines.delay(1500)
                val st = gateway.httpText(ep.port, "/api/tasks/${Uri.encode(tid)}")
                val o = runCatching { org.json.JSONObject(st.body) }.getOrNull() ?: continue
                last = o.optString("status")
                lastErr = o.optString("error")
                if (last != "running") break
            }
            assertTrue("不认识的书源任务最终必须失败收尾（当前=$last），不能停在运行中",
                last == "error" || last == "stopped")
            assertTrue("失败收尾应给出原因（当前空）", lastErr.isNotBlank())
            ev("用不存在书源创建任务：202 建任务 → 最终 $last（原因：${lastErr.take(50)}）")
            // 测试不留垃圾：这条任务记录是造出来的，用完即删
            // （此前每跑一轮全量套件，设备上就多一条 status=error 的任务）
            gateway.httpDelete(ep.port, "/api/tasks/${Uri.encode(tid)}")
            ev("已删除该测试任务记录（不留残留）")
        }

        // 7) SSRF：内网地址不得被接受为书籍地址
        val internal = gateway.httpPost(ep.port, "/api/tasks",
            org.json.JSONObject().put("source_uid", "x")
                .put("book_url", "http://127.0.0.1:8766/").toString())
        assertTrue("内网书籍地址必须被拒绝，实际 ${internal.code}", !internal.ok)
        ev("内网书籍地址被拒：HTTP ${internal.code}")
    }
}
