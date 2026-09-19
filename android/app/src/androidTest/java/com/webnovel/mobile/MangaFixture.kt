package com.webnovel.mobile

import android.content.Context
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.delay
import org.json.JSONObject
import java.io.File
import java.net.URLEncoder

/**
 * 韧性用例的**漫画夹具解析器**：把"用哪部漫画、哪一话"从每次搜索里解耦出来。
 *
 * 为什么需要（2026-09-15 实测）：下载/强杀/锁屏/Doze/覆盖升级这些用例关心的是
 * **App 自己的行为**（任务状态、文件完整性、恢复），却都被"搜索一次真实源"绑住。
 * MangaDex 在连续跑了几轮整包套件后会限流，搜索返回空 → 一批用例成片失败
 * （实测：一轮 54 条里 5 条失败，全部是"搜索没有 mangadex 的结果"或它的连带失败）。
 * 而**详情/图片接口**在同一时段一直是好的。
 *
 * 所以：
 *   1. 优先读缓存（应用私有目录里的 manga-fixture.json）——只校验详情是否仍可解析，
 *      不重新搜索；后续用例直接用同一部作品，稳定且更快；
 *   2. 缓存没有/失效时才搜索，并且**按源限定 + 换关键词 + 退避重试**；
 *   3. 拿到可用作品后落盘缓存，供本轮与后续用例复用。
 *
 * 它只影响测试：不参与产品逻辑，也不改变任何接口契约。
 */
object MangaFixture {

    data class Pick(val source: String, val comicId: String, val chapterId: String,
                    val title: String, val cover: String, val fromCache: Boolean)

    private const val CACHE = "manga-fixture.json"
    private val KEYWORDS = listOf("巨人", "海贼王", "火影忍者")

    private fun ctx(): Context = InstrumentationRegistry.getInstrumentation().targetContext

    private fun cacheFile(): File = File(ctx().filesDir, CACHE)

    /** 解析一部可下载的漫画（优先缓存；失败才搜索并重试） */
    suspend fun pick(gw: EngineGateway, port: Int,
                     source: String = "mangadex",
                     log: (String) -> Unit = {}): Pick {
        loadCached()?.let { c ->
            val ok = fromDetail(gw, port, c.source, c.comicId)?.let { p ->
                p.copy(chapterId = if (c.chapterId.isNotBlank()) c.chapterId else p.chapterId)
            }
            if (ok != null && ok.chapterId.isNotBlank()) {
                log("夹具命中缓存：${ok.source}/${ok.comicId}（未重新搜索）")
                return ok.copy(fromCache = true)
            }
            log("缓存已失效（详情解析不出来），改为搜索")
        }
        val sources = listOf(source, "jm", "copymanga", "nhentai").distinct()
        for (round in 0 until 3) {
            for (src in sources) {
                for (kw in KEYWORDS) {
                    val hit = searchOne(gw, port, src, kw) ?: continue
                    val p = fromDetail(gw, port, hit.first, hit.second) ?: continue
                    if (p.chapterId.isBlank()) continue
                    save(p)
                    log("夹具新解析：${p.source}/${p.comicId}（关键词「$kw」，第 ${round + 1} 轮）")
                    return p.copy(fromCache = false)
                }
            }
            delay(4000L * (round + 1))          // 退避：给源站喘口气
            log("第 ${round + 1} 轮没拿到可用作品，退避后再试")
        }
        // 源站不可达/限流时**跳过**而不是判失败：这类失败会把发布门槛变成噪声
        // （"红"看起来像代码回归，实际是外站天气）。用 Assume 跳过，报告里会显示
        // skipped 与原因，代码回归仍会是红。
        log("三个源、三个关键词、三轮退避都没拿到可用作品：判定为源站不可达/限流，跳过本用例")
        org.junit.Assume.assumeTrue(
            "源站不可达或限流（三源×三关键词×三轮退避全失败）——环境问题，跳过",
            false)
        error("unreachable")
    }

    private fun loadCached(): Pick? {
        val f = cacheFile()
        if (!f.isFile) return null
        return runCatching {
            val o = JSONObject(f.readText(Charsets.UTF_8))
            val s = o.optString("source"); val c = o.optString("comic_id")
            if (s.isBlank() || c.isBlank()) null
            else Pick(s, c, o.optString("chapter_id"), o.optString("title"),
                o.optString("cover"), true)
        }.getOrNull()
    }

    private fun save(p: Pick) {
        runCatching {
            cacheFile().writeText(JSONObject()
                .put("source", p.source).put("comic_id", p.comicId)
                .put("chapter_id", p.chapterId).put("title", p.title)
                .put("cover", p.cover).put("ts", System.currentTimeMillis())
                .toString(), Charsets.UTF_8)
        }
    }

    /** 只查详情（比搜索稳得多）：拿标题/封面/章节 */
    private suspend fun fromDetail(gw: EngineGateway, port: Int,
                                   source: String, comicId: String): Pick? {
        val dr = gw.httpText(port, "/api/manga/$source/$comicId")
        if (!dr.ok) return null
        val d = EngineData.mangaDetail(dr.body) ?: return null
        val ch = d.chapters.lastOrNull() ?: return null
        return Pick(source, comicId, ch.id, d.title, d.cover, false)
    }

    /** 指定源 + 关键词搜索（限定源比全源搜索省得多，也更不容易被限流） */
    private suspend fun searchOne(gw: EngineGateway, port: Int, source: String,
                                  keyword: String): Pair<String, String>? {
        val r = gw.httpText(port, "/api/manga/search?source=$source&q=" +
            URLEncoder.encode(keyword, "UTF-8"))
        if (!r.ok) return null
        val (hits, _) = EngineData.mangaSearch(r.body)
        val h = hits.firstOrNull { it.source == source } ?: hits.firstOrNull() ?: return null
        return h.source to h.comicId
    }
}
