package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 漫画「排行/分类浏览」真机验收（漫画优先）。
 *
 * 覆盖界面点分类按钮时走的**同一条路径**：
 *   GET /api/explore/sources        → 漫画侧源与分类清单（含适配器自己声明的分组）
 *   GET /api/manga/browse?source=&category=  → 列表
 *
 * 口径要求（不编造数据）：
 *   - 探索清单只含**自己实现了 categories()** 的适配器；
 *   - 分组必须来自适配器（排行 / 分类），界面照原样显示；
 *   - 未知分类 → 404 且给出可读原因，而不是返回别的分类冒充；
 *   - 真实取数允许因网络失败，但成功时条目结构必须完整（id/标题/封面）。
 */
@RunWith(AndroidJUnit4::class)
class MangaBrowsePathTest {

    private lateinit var gateway: EngineGateway

    private fun ev(line: String) = println("MANGA_BROWSE_EVIDENCE $line")

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

    private fun errOf(body: String): String =
        runCatching { JSONObject(body).optString("error") }.getOrDefault("")

    @Test
    fun mangaBrowse_isHonestAndUsable() = runBlocking {
        val ep = gateway.currentEndpoint()!!

        // 1) 探索清单：漫画侧必须按适配器实况报告
        val r = gateway.httpText(ep.port, "/api/explore/sources")
        assertTrue("探索源接口状态码=${r.code}", r.ok)
        val sources = EngineData.exploreMangaSources(r.body)
        val (supported, reason) = EngineData.exploreMangaNote(r.body)
        assertTrue("声明支持时必须给出源与分类", !supported || sources.isNotEmpty())
        assertFalse("不该出现没有分类的源",
            sources.any { it.categories.isEmpty() })
        for (s in sources) {
            for (c in s.categories) {
                assertTrue("分类标题不能为空：${s.name}", c.title.isNotBlank())
                assertTrue("分类 key 不能为空：${s.name}/${c.title}", c.url.isNotBlank())
            }
        }
        ev("漫画探索源=" + sources.map { "${it.name}(${it.categories.size})" } +
            " supported=$supported")
        if (reason.isNotBlank()) ev("漫画侧说明=${reason.take(60)}")

        // 2) 每个源逐个真取一次（点按钮 → 列表），成功则校验结构
        var okCount = 0
        for (s in sources) {
            val cat = s.categories.first()
            val b = gateway.httpText(ep.port,
                "/api/manga/browse?source=${android.net.Uri.encode(s.key)}" +
                    "&category=${android.net.Uri.encode(cat.url)}")
            if (!b.ok) {
                val why = errOf(b.body)
                assertTrue("取数失败必须给可读原因（实际='$why'）", why.isNotBlank())
                ev("${s.name} · ${cat.title} 失败（如实报错）：HTTP ${b.code} · ${why.take(60)}")
                continue
            }
            val hits = EngineData.mangaSearch(b.body).first
            for (h in hits) {
                assertTrue("条目必须有 id：${s.name}", h.comicId.isNotBlank())
                assertTrue("条目必须有标题：${s.name}", h.title.isNotBlank())
                assertTrue("条目必须带源 key", h.source == s.key)
            }
            okCount++
            ev("${s.name} · ${cat.title} → ${hits.size} 部（结构完整，封面例=" +
                (hits.firstOrNull()?.cover?.take(64) ?: "无") + "）")
        }
        ev("可浏览源 ${okCount}/${sources.size}")

        // 3) 禁漫（若已在清单里）：分组口径与排行/分类两类入口都必须成立
        val jm = sources.firstOrNull { it.key == "jm" }
        if (jm != null) {
            val groups = jm.categories.map { it.group }.distinct().filter { it.isNotBlank() }
            ev("禁漫天堂分组=${groups} 入口数=${jm.categories.size}")
            assertTrue("禁漫的排行入口必须存在",
                jm.categories.any { it.url.startsWith("o:") })
            val orders = jm.categories.count { it.url.startsWith("o:") }
            assertEquals("禁漫排行入口应为 6 个（最近更新/最多观看/最多图片/最多点赞/评分/评论）",
                6, orders)

            // 排行入口真取（设备网络下 jm 需动态域名，允许失败但必须可读）
            val top = gateway.httpText(ep.port,
                "/api/manga/browse?source=jm&category=" +
                    android.net.Uri.encode("o:mv"))
            if (top.ok) {
                val hits = EngineData.mangaSearch(top.body).first
                assertTrue("禁漫排行应返回条目", hits.isNotEmpty())
                assertTrue("禁漫条目 id 应为数字码", hits.all { it.comicId.matches(Regex("\\d+")) })
                assertTrue("禁漫封面应带图床域名", hits.all { it.cover.startsWith("http") })
                ev("禁漫排行 o:mv → ${hits.size} 部，例=${hits[0].title.take(24)}")
            } else {
                assertTrue("禁漫排行失败必须给可读原因", errOf(top.body).isNotBlank())
                ev("禁漫排行失败（如实报错）：HTTP ${top.code} · ${errOf(top.body).take(60)}")
            }

            // 分类入口（依赖 /categories 接口；设备上拿不到就只保留排行——如实记录）
            val catKeys = jm.categories.filter { it.url.startsWith("c:") }
            ev("禁漫分类入口=${catKeys.size} 个" +
                (if (catKeys.isEmpty()) "（设备上未取到分类清单）" else "，例=${catKeys[0].title}"))
            if (catKeys.isNotEmpty()) {
                val c = catKeys.first()
                val b = gateway.httpText(ep.port,
                    "/api/manga/browse?source=jm&category=" +
                        android.net.Uri.encode(c.url))
                if (b.ok) {
                    val hits = EngineData.mangaSearch(b.body).first
                    assertTrue("禁漫分类「${c.title}」应返回条目", hits.isNotEmpty())
                    ev("禁漫分类 ${c.title} → ${hits.size} 部，例=${hits[0].title.take(24)}")
                } else {
                    assertTrue("分类取数失败必须给可读原因", errOf(b.body).isNotBlank())
                    ev("禁漫分类「${c.title}」失败：HTTP ${b.code} · ${errOf(b.body).take(60)}")
                }
            }
        } else {
            ev("禁漫天堂不在漫画探索清单里（未声明分类或适配器未注册）")
        }

        // 4) 未知分类：必须 404，不许拿默认列表冒充
        if (sources.isNotEmpty()) {
            val bad = gateway.httpText(ep.port,
                "/api/manga/browse?source=${sources[0].key}&category=c:__不存在__")
            assertEquals("未知分类应 404", 404, bad.code)
            assertTrue("404 必须给可读原因", errOf(bad.body).isNotBlank())
            ev("未知分类 → HTTP ${bad.code} · ${errOf(bad.body).take(50)}")
        }

        // 5) 没声明分类的源也必须明确拒绝（不返回空壳 200）
        val noCat = gateway.httpText(ep.port, "/api/manga/browse?source=nhentai")
        assertTrue("未声明分类的源不应 2xx（实际 ${noCat.code}）", !noCat.ok)
        ev("未声明分类的源（nhentai）→ HTTP ${noCat.code} · ${errOf(noCat.body).take(50)}")
    }
}
