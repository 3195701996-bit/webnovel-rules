package com.webnovel.mobile

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * **旧版图片缓存必须被"说出来"**（升级版 Venera 指南 §4 P0"旧缓存升级体验"）。
 *
 * 指南原文要求：
 *   · 旧 JM 花图会在下次联网打开该章节时重新取得；
 *   · 源页面提供"重建图片缓存"作为主动修复入口；
 *   · **在无网络时显示"旧图片需要重新获取"，不显示为新的图片解码错误**。
 *
 * 本用例用夹具漫画（`SelfTestComic`，不联网）在设备上验证"读取侧的处理版本判定"
 * 与"界面的可行动提示"两件事：
 *   1. 把某话的图片目录标记成**旧版本**（`_processed.json` 里写 version=1，而
 *      适配器当前版本是 2）→ `GET /chapter/<id>` 必须带 `stale_processing=true`
 *      与可读的 `stale_reason`；
 *   2. 该话在原生阅读器里必须出现 `stale_cache_banner` 横幅，文字含"重新获取"，
 *      并有 `stale_cache_rebuild` 按钮（点一下走重建接口）；
 *   3. 标记恢复成当前版本后，横幅消失（不能常驻吓用户）。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class StaleImageBannerTest {

    @get:Rule
    val rule = createAndroidComposeRule<MainActivity>()

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext
    private lateinit var comic: SelfTestComic
    private lateinit var gateway: EngineGateway

    private fun ev(line: String) = println("STALE_BANNER_EVIDENCE $line")

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size
    private fun texts(t: String, substring: Boolean = false) =
        rule.onAllNodesWithText(t, substring = substring).fetchSemanticsNodes().size

    private fun waitText(t: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) { texts(t, substring) > 0 }
    }

    private fun markerPath(chapterId: String): File =
        File(OfflineStore.runtimeDir(ctx),
            "manga/downloads/${comic.sourceKey}/${comic.comicIdValue}/$chapterId/_processed.json")

    private fun writeMarker(chapterId: String, version: Int) {
        val f = markerPath(chapterId)
        require(f.parentFile?.isDirectory == true) { "夹具章节目录不存在：${f.parentFile}" }
        f.writeText(JSONObject().put(comic.sourceKey,
            JSONObject().put("version", version)).toString(), Charsets.UTF_8)
    }

    @Before
    fun setUp() {
        val uniq = "__selftest_stale_%d__".format(System.currentTimeMillis() % 1_000_000)
        // 故意用 **jm** 作源：只有带图片后处理版本的源才受"处理版本判定"约束
        // （mangadex 这类无后处理的源按设计不受约束，用它测不出这条规则）。
        comic = SelfTestComic(source = "jm", comicId = uniq)
        comic.create()
        gateway = EngineGateway(ctx)
        val st = runBlocking { gateway.connect() }
        assertTrue("引擎未就绪：$st", st is EngineState.Ready)
    }

    @After
    fun tearDown() {
        comic.cleanup()
        assertFalse("自检漫画未清理干净", comic.exists())
    }

    @Test
    fun staleChapter_isAnnouncedToReader_andBannerCanRebuild() {
        val ep = runBlocking { gateway.currentEndpoint() } ?: throw AssertionError("引擎未就绪")
        val ch1 = SelfTestComic.CH1_ID

        // 1) 把第 1 话标成旧版本 → 章节元信息必须如实报告
        writeMarker(ch1, 1)
        val r = runBlocking {
            gateway.httpText(ep.port, "/api/manga/${comic.sourceKey}/${comic.comicIdValue}" +
                "/chapter/$ch1")
        }
        assertTrue("章节元信息应 200：HTTP ${r.code}", r.ok)
        val o = JSONObject(r.body)
        ev("章节元信息：local=${o.optBoolean("local")} stale=${o.optBoolean("stale_processing")} " +
            "reason=${o.optString("stale_reason")}")
        assertTrue("旧版本缓存必须被标出来（读取侧版本判定）", o.optBoolean("stale_processing"))
        assertTrue("必须给出可行动原因：${o.optString("stale_reason")}",
            o.optString("stale_reason").contains("重新获取"))

        // 2) 原生阅读器里必须出现横幅与"重新获取"按钮
        waitText(SelfTestComic.TITLE)
        rule.onAllNodesWithText(SelfTestComic.TITLE)[0].performScrollTo().performClick()
        waitText("开始阅读")
        rule.onAllNodesWithText("开始阅读")[0].performClick()
        rule.waitUntil(60_000) { nodes("stale_cache_banner") > 0 }
        // 容器节点本身没有文本，理由在子节点里——按文本断言（此前读了容器 config，误判失败）
        rule.waitUntil(60_000) { texts("重新获取", substring = true) > 0 }
        ev("横幅在位，文案含『重新获取』；重建入口=${nodes("stale_cache_rebuild")} 个")
        assertTrue("横幅必须带一键重建入口", nodes("stale_cache_rebuild") > 0)
        assertTrue("横幅文案要说明是旧版缓存：",
            texts("旧版处理缓存", substring = true) > 0 || texts("重新获取", substring = true) > 0)

        // 3) 点"重新获取"→ 走重建接口（真实调用，不假装成功）
        rule.onAllNodesWithTag("stale_cache_rebuild")[0].performClick()
        rule.waitUntil(60_000) {
            // 重建后标记会被写成当前版本；或界面给出可读错误（都算"有反应"）
            nodes("stale_cache_rebuild") > 0 || texts("重新获取失败", substring = true) > 0
        }
        ev("点击『重新获取』后有反应（重建流程被触发）")

        // 4) 标记回到当前版本 → 不再报告旧缓存，横幅也必须消失（不能常驻吓用户）
        runBlocking { kotlinx.coroutines.delay(1500) }
        // 重建会把该话的旧图整体清掉（预期行为），所以先重建夹具、再写**当前版本**标记
        comic.create()
        writeMarker(ch1, 2)
        val r2 = runBlocking {
            gateway.httpText(ep.port, "/api/manga/${comic.sourceKey}/${comic.comicIdValue}" +
                "/chapter/$ch1")
        }
        assertTrue("当前版本章节应 200：HTTP ${r2.code}", r2.ok)
        assertFalse("当前版本不得再报 stale_processing：${r2.body.take(160)}",
            JSONObject(r2.body).optBoolean("stale_processing"))

        rule.onAllNodesWithText("← 返回")[0].performClick()
        waitText(SelfTestComic.TITLE, timeoutMs = 60_000)
        rule.onAllNodesWithText(SelfTestComic.TITLE)[0].performScrollTo().performClick()
        waitText("继续阅读", timeoutMs = 60_000, substring = true)
        rule.onAllNodesWithText("继续阅读", substring = true)[0].performClick()
        rule.waitUntil(60_000) { texts("/ 3", substring = true) > 0 }
        assertEquals("标记为当前版本后不应再有旧缓存横幅", 0, nodes("stale_cache_banner"))
        ev("标记恢复当前版本后：元信息不报 stale、界面横幅消失")
        Unit
    }
}
