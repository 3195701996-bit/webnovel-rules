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
import java.net.URLEncoder
import java.security.MessageDigest

/**
 * **小说最小回归源集**（0.51.0 路线 §7 P1-3）：
 * "将目前'部分源可用'固化为具名的最小回归源集；每次漫画改动不得降低它们的搜索/阅读结果。"
 *
 * 背景：漫画改动（图片管线、分源搜索、任务装载…）都会碰到共享的 HTTP/配置/存储路径。
 * 用户当前**只有小说部分源可用**，所以这条线必须有一组**具名**的源，每次跑套件都要
 * 验证 搜索 → 目录 → 正文 三步不退化；失败要**指名到源与阶段**，不许含糊地说"搜索没结果"。
 *
 * 选取口径（写死在代码里 + 归档记录 `03-验证结果/小说最小回归源集-*.md`，便于评审核对）：
 *   · 必须由**设备实测**选出（探针 NovelSourceProbeTest 的当轮证据），不照抄历史记录；
 *   · 尽量来自**不同站点**（不同域名/不同解析），避免一个站挂掉就整组失败；
 *   · 每个源记录**当轮命中关键词**——源站内容差异导致的"这个词没结果"不算退化。
 *
 * 三步用的都是 **App 真实走的接口**（不用测试专用捷径）：
 *   1) 搜索   GET  /api/search?source=<uid>&q=<kw>
 *   2) 目录   POST /api/search-toc  {source_uid, book_url}   ← 详情页章节列表
 *   3) 正文   POST /api/tasks {source_uid, book_url} → GET /api/books/<key>/chapter/1
 *      （阅读正文的前提是书在书架里：与用户操作路径一致）
 *
 * 副作用纪律：只对**本用例新建**的任务/书籍做清理（先 stop 再 DELETE），
 * 测试前已存在的书/任务一律不碰。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class NovelRegressionTest {

    private data class RegSource(
        val uid: String,
        val label: String,
        /** 当轮实测命中的关键词，按优先顺序；换词需同步归档文档 */
        val keywords: List<String>,
    )

    /**
     * 最小回归源集（改动这里必须同步更新 `报告归档/03-验证结果/小说最小回归源集-*.md`）。
     *
     * 当轮选源证据（2026-09-16 设备探针 NovelSourceProbeTest，候选 17 个 → 搜索命中 12、目录可用 11）：
     *   · 入集 3 个——设备上**已启用**、搜索与目录当轮都通过，且分属**三个不同域名**：
     *     爱下电子书 ixdzs8.com（1278 章）、精华书阁 m.jhsssd.com（605 章）、啃书网 kenshuzw.la（1396 章）。
     *   · 同轮可用但**未入最小集**（备用，不参与断言）：大帝书阁 ddsk.la/dingdiansk.com（1306 章）、
     *     思路客 isiluke.la（1304 章）、顶点 m.23uswx.la（1304 章）、全本 quanbenw.com（218 章，已停用）。
     *   · 明确**不入集**：quanben.io（已启用但搜索对「剑来/斗破苍穹」0 结果）、
     *     笔趣阁新 xinbqg.org（搜索 ok 但目录 HTTP 500）、aixiaxsw.com（搜索 0 结果）。
     */
    private val regressionSet = listOf(
        RegSource("爱下书_ixdzs8__ixdzs8.com", "爱下电子书(ixdzs8)", listOf("剑来")),
        RegSource("精华书阁_m.jhsssd.com_", "精华书阁(m.jhsssd.com)", listOf("剑来")),
        RegSource("啃书网_kenshuwx__www.kenshuzw.la", "啃书网(kenshuzw.la)", listOf("剑来")),
    )

    private fun ev(line: String) = println("NOVEL_REGRESSION_EVIDENCE $line")

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8")

    /** 与 engine/app_utils.book_key_of 同口径：<uid>_<md5(book_url)[:10]> */
    private fun bookKeyOf(uid: String, bookUrl: String): String {
        val md5 = MessageDigest.getInstance("MD5").digest(bookUrl.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
        return "${uid}_${md5.take(10)}"
    }

    @Test
    fun frozenNovelSources_stillSearchTocAndRead(): Unit = runBlocking {
        val gw = EngineGateway(ctx)
        val ep = (gw.connect() as? EngineState.Ready)?.endpoint
            ?: throw AssertionError("引擎未就绪")
        val failures = ArrayList<String>()
        for (src in regressionSet) {
            var stage = "search"
            var detail = ""
            var createdTaskId: String? = null
            var createdBookKey: String? = null
            try {
                // 1) 搜索：限定该源（分源搜索契约见 PerSourceSearchUiTest）
                //
                // 选哪一条**不能只取第一条**：源站的模糊匹配会把同名前缀的书排到前面。
                // 2026-09-17 实测：ixdzs8 对「剑来」的第一条变成《青冥等剑来》（89 章），
                // 《剑来》本身（1278 章）掉到第 6 条——按"第一条"判定会把它误报成
                // "目录退化"，正是指南禁止的"把源站行为写成代码退化"。
                // 因此收集该源的全部候选后，优先取标题精确等于关键词的，其次包含关键词的，
                // 最后才回退第一条，并把选择理由打印进证据。
                var hit: Pair<String, String>? = null     // book_url to book name
                var usedKw = ""
                var pickedHow = ""
                for (kw in src.keywords) {
                    val r = gw.httpText(ep.port,
                        "/api/search?source=${enc(src.uid)}&q=${enc(kw)}")
                    if (!r.ok) { detail = "HTTP ${r.code}"; continue }
                    val o = runCatching { JSONObject(r.body) }.getOrNull() ?: continue
                    if (o.has("error")) detail = o.optString("error").take(60)
                    val groups = o.optJSONArray("groups") ?: continue
                    val cands = ArrayList<Pair<String, String>>()
                    for (i in 0 until groups.length()) {
                        val g = groups.optJSONObject(i) ?: continue
                        val ss = g.optJSONArray("sources") ?: continue
                        for (j in 0 until ss.length()) {
                            val s = ss.optJSONObject(j) ?: continue
                            if (s.optString("source_uid") == src.uid) {
                                cands.add(s.optString("book_url") to g.optString("name"))
                            }
                        }
                    }
                    if (cands.isEmpty()) { detail = "「$kw」0 结果"; continue }
                    val exact = cands.firstOrNull { it.second.trim() == kw }
                    val contains = cands.firstOrNull { it.second.contains(kw) }
                    hit = exact ?: contains ?: cands[0]
                    usedKw = kw
                    pickedHow = when {
                        exact != null -> "标题精确匹配（候选第 ${cands.indexOf(exact) + 1} 条）"
                        contains != null -> "标题包含关键词（候选第 ${cands.indexOf(contains) + 1} 条）"
                        else -> "回退第一条（无标题匹配；源站可能改版）"
                    }
                    ev("【${src.label}】「$kw」候选 ${cands.size} 条；选中 ${hit.second} — $pickedHow")
                    break
                }
                assertTrue("【${src.label}】搜索阶段失败：${src.keywords.joinToString("/")} 都没结果（$detail）",
                    hit != null)
                val (bookUrl, bookName) = hit!!
                val key = bookKeyOf(src.uid, bookUrl)

                // 2) 目录：详情页用的 /api/search-toc（不是书架里的 /api/books/<key>）
                stage = "toc"
                val tocBody = JSONObject().put("source_uid", src.uid)
                    .put("book_url", bookUrl).toString()
                val tr = gw.httpPost(ep.port, "/api/search-toc", tocBody)
                assertTrue("【${src.label}】目录阶段失败：HTTP ${tr.code} ${tr.body.take(80)}", tr.ok)
                val chapters = runCatching {
                    JSONObject(tr.body).optJSONArray("chapters") ?: org.json.JSONArray()
                }.getOrDefault(org.json.JSONArray())
                assertTrue("【${src.label}】目录为空（书=$bookName，关键词「$usedKw」）",
                    chapters.length() >= 3)

                // 3) 正文：加入书架（与用户操作同路径）→ 等第一章抓下来 → 读正文
                stage = "content"
                // 判据不是"书在不在书架"，而是"第一章能不能读"：
                // 上一轮清理被运行中的任务线程覆盖时，会出现"有 state、无缓存"的中间态。
                var firstDownloaded = false
                var path = "新建任务"
                val d0 = gw.httpText(ep.port, "/api/books/${enc(key)}")
                if (d0.ok) {
                    firstDownloaded = runCatching {
                        JSONObject(d0.body).optJSONArray("chapters")?.optJSONObject(0)
                            ?.optBoolean("downloaded") ?: false
                    }.getOrDefault(false)
                    if (firstDownloaded) path = "复用书架已下载的首章"
                }
                if (!firstDownloaded) {
                    val tb = JSONObject().put("source_uid", src.uid)
                        .put("book_url", bookUrl).toString()
                    val cr = gw.httpPost(ep.port, "/api/tasks", tb)
                    assertTrue("【${src.label}】加入书架失败：HTTP ${cr.code} ${cr.body.take(80)}",
                        cr.code == 202 || cr.code == 409)
                    if (cr.code == 202) {
                        createdTaskId = runCatching {
                            JSONObject(cr.body).optString("id").ifBlank { null }
                        }.getOrNull()
                    } else {
                        // 409 带的是"已在运行的任务 id"（服务端按 源+书地址 去重）：
                        // 它同样在写这本书的目录，清理前必须一起停掉
                        path = "已有任务（409）"
                        createdTaskId = runCatching {
                            JSONObject(cr.body).optString("task_id").ifBlank { null }
                        }.getOrNull()
                    }
                    createdBookKey = key
                    // 抓第一章需要时间（源站慢时更久）：最多等 180s
                    val deadline = System.currentTimeMillis() + 180_000
                    var lastLog = ""
                    while (System.currentTimeMillis() < deadline) {
                        val dr = gw.httpText(ep.port, "/api/books/${enc(key)}")
                        if (dr.ok) {
                            val chs = runCatching { JSONObject(dr.body).optJSONArray("chapters") }
                                .getOrNull()
                            if (chs != null && chs.length() > 0) {
                                val c0 = chs.optJSONObject(0)
                                lastLog = "章数=${chs.length()} 首章downloaded=" +
                                    "${c0?.optBoolean("downloaded")} 失败原因=" +
                                    c0?.optString("failed_reason").orEmpty().take(60)
                                if (c0?.optBoolean("downloaded") == true) {
                                    firstDownloaded = true; break
                                }
                            } else {
                                lastLog = "书籍状态尚未落盘"
                            }
                        } else {
                            lastLog = "HTTP ${dr.code}"
                        }
                        delay(3000)
                    }
                    assertTrue("【${src.label}】正文阶段失败：180s 内第一章未下载完成（$lastLog）",
                        firstDownloaded)
                }
                val cr2 = gw.httpText(ep.port, "/api/books/${enc(key)}/chapter/1")
                assertTrue("【${src.label}】正文阶段失败：HTTP ${cr2.code}", cr2.ok)
                val content = JSONObject(cr2.body).optString("content")
                assertTrue("【${src.label}】正文为空或过短（${content.length} 字）", content.length >= 300)
                // 长度够不等于"是正文"（可能是提示页/反爬文案）：留正文开头给人核对
                val preview = content.replace(Regex("\\s+"), " ").trim().take(70)

                ev("${src.label}：搜索 ok（「$usedKw」→《${bookName.take(16)}》）" +
                    "→ 目录 ${chapters.length()} 章 → 正文 ${content.length} 字（$path）｜开头：「$preview」")
            } catch (t: Throwable) {
                failures.add("${src.label}（$stage）：${t.message}")
                ev("❌ ${src.label} 在 $stage 阶段失败：${t.message}")
            } finally {
                // 清理只针对本用例新建的任务/书籍。**必须先等任务真的停下来**：
                // stop 只是置标志，抓取线程还在写书目录；此时删书会被线程用内存状态
                // 把目录写回，留下"章标已下载、缓存却没有"的脏状态（0.55.0 实测踩到）。
                // 判据：detail 的 done（磁盘缓存数）连续两次不变 = 写入已静止。
                if (createdTaskId != null) {
                    runCatching { gw.httpPost(ep.port, "/api/tasks/${enc(createdTaskId!!)}/stop") }
                    runCatching { gw.httpDelete(ep.port, "/api/tasks/${enc(createdTaskId!!)}") }
                    if (createdBookKey != null) {
                        var last = -1
                        var stable = 0
                        val q = System.currentTimeMillis() + 45_000
                        while (System.currentTimeMillis() < q && stable < 2) {
                            val r = gw.httpText(ep.port, "/api/books/${enc(createdBookKey!!)}")
                            val done = if (r.ok) runCatching {
                                JSONObject(r.body).optInt("done", -1)
                            }.getOrDefault(-1) else -2
                            if (done == last && done >= 0) stable++ else { stable = 0; last = done }
                            delay(3000)
                        }
                        if (stable < 2) ev("⚠ ${createdBookKey} 在 45s 内仍在写入（done=$last），删除前状态可能不干净")
                    }
                }
                if (createdBookKey != null) {
                    var gone = false
                    for (attempt in 1..4) {
                        if (attempt > 1) runCatching {
                            gw.httpDelete(ep.port, "/api/books/${enc(createdBookKey!!)}")
                        }
                        delay(2500)
                        if (!gw.httpText(ep.port, "/api/books/${enc(createdBookKey!!)}").ok) {
                            gone = true
                            break
                        }
                    }
                    if (!gone) ev("⚠ 清理未完成：任务线程仍在写 ${createdBookKey}（保留在书架，本轮不谎报已清理）")
                    // 软删除会留在 runtime/trash 里：清掉本轮产生的条目（测试不留垃圾）
                    val trash = java.io.File(ctx.filesDir, "runtime/trash")
                    trash.listFiles()?.filter { it.name.startsWith(createdBookKey!!) }
                        ?.forEach { it.deleteRecursively() }
                }
            }
            delay(500)
        }
        ev("回归源集：${regressionSet.size} 个，失败 ${failures.size} 个")
        assertTrue("小说最小回归源集不得退化（用户当前只有小说部分可用）——" +
            "失败明细：\n" + failures.joinToString("\n"), failures.isEmpty())
    }
}
