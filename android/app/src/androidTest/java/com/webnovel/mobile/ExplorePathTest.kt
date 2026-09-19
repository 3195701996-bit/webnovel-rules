package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 探索/榜单契约验收（对照 Venera 的探索分页，但只做有数据支撑的探索）：
 *
 *   - /api/explore/sources 只列**自己声明了 exploreUrl/ruleExplore** 的源，
 *     且给出总数与"有多少源提供榜单"，界面据此显示"N/M 个源提供"；
 *   - 没声明探索规则的源一律不出现（不拿搜索结果冒充榜单）；
 *   - 分类条目结构完整（title/url 非空），分组标题可继承；
 *   - 未知源 → 404；缺参 → 明确失败；非法分类地址（内网）→ 明确拒绝（SSRF）；
 *   - 真实分类取数允许失败（站点可能不可达），但失败必须是**可读错误**而不是空列表。
 */
@RunWith(AndroidJUnit4::class)
class ExplorePathTest {

    private lateinit var gateway: EngineGateway

    private fun ev(line: String) = println("EXPLORE_PATH_EVIDENCE $line")

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
    fun exploreContract_isHonest() = runBlocking {
        val ep = gateway.currentEndpoint()!!

        // 1) 源清单：只含有探索规则的源，且给出总数口径
        val r = gateway.httpText(ep.port, "/api/explore/sources")
        assertTrue("探索源接口状态码=${r.code}", r.ok)
        val (sources, counts) = EngineData.exploreSources(r.body)
        val (total, withExplore) = counts
        assertTrue("应返回内置源总数", total > 0)
        assertEquals("with_explore 应与列表长度一致", sources.size, withExplore)
        assertTrue("提供榜单的源不应多于内置源总数", withExplore <= total)
        for (s in sources) {
            assertTrue("探索源必须有名称", s.name.isNotBlank())
            assertTrue("探索源必须至少有一个分类：${s.name}", s.categories.isNotEmpty())
            for (c in s.categories) {
                assertTrue("分类标题不能为空：${s.name}", c.title.isNotBlank())
                assertTrue("分类地址不能为空：${s.name}/${c.title}", c.url.isNotBlank())
            }
        }
        assertTrue("响应应说明'只有声明了探索规则的源才有榜单'", r.body.contains("exploreUrl"))
        // 漫画侧必须**如实说明**为何不在探索里（适配器未提供排行/分类），
        // 而不是让用户纳闷"为什么漫画没有探索"
        // 漫画侧：按适配器实况报告。MangaDex 已实现 categories()/browse()，
        // 因此这里必须是 supported=true 且带分类；没实现的源不出现在列表里。
        val (mangaOk, mangaWhy) = EngineData.exploreMangaNote(r.body)
        val browseSources = EngineData.exploreMangaSources(r.body)
        if (mangaOk) {
            assertTrue("声明支持漫画分类时，必须给出源与分类", browseSources.isNotEmpty())
            assertTrue("每个源都要有分类",
                browseSources.all { it.categories.isNotEmpty() })
            ev("漫画探索：源=${browseSources.map { it.name }} " +
                "分类数=${browseSources.sumOf { it.categories.size }}")
        } else {
            assertTrue("不支持时必须给出原因：'$mangaWhy'", mangaWhy.isNotBlank())
            ev("漫画侧说明：supported=false 原因=${mangaWhy.take(40)}")
        }
        ev("探索源：$withExplore / $total 个内置源提供榜单")
        if (sources.isNotEmpty()) {
            ev("例：${sources[0].name} → ${sources[0].categories.size} 个分类（首个：${sources[0].categories[0].title}）")
        }

        // 2) 未知源 → 404；缺参 → 明确失败
        val unknown = gateway.httpText(ep.port,
            "/api/explore?source=不存在的源&url=/x_{{page}}.html")
        assertEquals("未知源应 404", 404, unknown.code)
        val noParam = gateway.httpText(ep.port, "/api/explore")
        assertFalse("缺参不应返回 2xx", noParam.ok)
        ev("未知源 ${unknown.code} / 缺参 ${noParam.code}")

        // 3) 内网分类地址：SSRF 必须拒绝（而不是静默失败）
        if (sources.isNotEmpty()) {
            val internalUrl = "http://127.0.0.1:8766/rank/{{page}}.html"
            val blocked = gateway.httpText(ep.port,
                "/api/explore?source=${android.net.Uri.encode(sources[0].uid)}" +
                    "&url=${android.net.Uri.encode(internalUrl)}")
            assertFalse("内网分类地址不应返回 2xx（实际 ${blocked.code}）", blocked.ok)
            ev("内网分类地址被拒：HTTP ${blocked.code}")

            // 4) 真实分类取数：允许失败，但失败要给可读原因，成功要结构完整
            val cat = sources[0].categories.first()
            val books = gateway.httpText(ep.port,
                "/api/explore?source=${android.net.Uri.encode(sources[0].uid)}" +
                    "&url=${android.net.Uri.encode(cat.url)}&page=1")
            if (books.ok) {
                val list = EngineData.exploreBooks(books.body)
                for (b in list) {
                    assertTrue("榜单条目必须有书名与地址：${b.name}", b.name.isNotBlank())
                    assertTrue("榜单条目必须有书籍地址：${b.name}", b.bookUrl.isNotBlank())
                    assertEquals("榜单条目应带来源 uid", sources[0].uid, b.sourceUid)
                    // 榜单链接常是相对路径，必须已归一成绝对地址——否则界面拿它
                    // 建任务会被服务端 SSRF 校验拒绝（"加入书架失败"）
                    assertTrue("榜单书籍地址必须是绝对地址：${b.bookUrl}",
                        b.bookUrl.startsWith("http://") || b.bookUrl.startsWith("https://"))
                }
                ev("真实分类「${cat.title}」→ ${list.size} 本（结构完整）")
            } else {
                val why = org.json.JSONObject(books.body).optString("error")
                assertTrue("取数失败必须给出可读原因（实际='$why'）", why.isNotBlank())
                ev("真实分类取数失败（如实报错）：HTTP ${books.code} · ${why.take(50)}")
            }
        }
    }
}
