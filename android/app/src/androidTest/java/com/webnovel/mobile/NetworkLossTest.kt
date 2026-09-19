package com.webnovel.mobile

import android.os.ParcelFileDescriptor
import android.provider.Settings
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.getOrNull
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TestRule
import org.junit.runner.Description
import org.junit.runner.RunWith
import org.junit.runners.model.Statement
import java.net.InetSocketAddress
import java.net.Socket

/**
 * 真断网（设备飞行模式）验收：方向基线 §7.2 P1-C 的"无网"一条，以及 §8 D
 * "完成后断网重启，已下载内容可打开"。
 *
 * 为什么必须用**真的断网**而不是"把引擎停掉"：停引擎只证明"本地文件能读"，
 * 不证明"没有网络时程序行为正确"。断网时真正会出问题的是**联网那一步**：
 *   · 搜索/详情/下载必须**如实说是本机没网**，不能把锅甩给源站
 *     （当前文案是"源站异常或限流"，用户手机没信号时会得到完全错误的结论）；
 *   · 必须有**确定的终态**（失败要出现），不能一直转圈；
 *   · 网络恢复后必须**不用重启**就能继续用（失败是环境问题，不是把程序弄坏了）；
 *   · 断网冷启动时，本机引擎（回环服务）与已下载内容必须照常可用，
 *     且**不得出现桌面网络提示**（"回环失效/更换主机地址"是阶段 C 修掉的老 bug，
 *     断网是最容易让它复发的场景）。
 *
 * 断网用 `cmd connectivity airplane-mode`（UiAutomation 以 shell 身份执行），
 * 并在用例前后各自校验"真的断了/真的通了"——否则用例会在有网环境下假通过。
 * 恢复网络的代码放在最外层规则的 finally 与 @After 两处，确保不会把设备留在断网状态。
 */
@Retention(AnnotationRetention.RUNTIME)
@Target(AnnotationTarget.FUNCTION)
annotation class OfflineAtStart

/** 飞行模式总闸：@OfflineAtStart 的用例在**启动 Activity 之前**就断网 */
class AirplaneRule : TestRule {
    override fun apply(base: Statement, description: Description): Statement =
        object : Statement() {
            override fun evaluate() {
                val wantOffline = description.getAnnotation(OfflineAtStart::class.java) != null
                if (wantOffline) Airplane.set(true)
                try {
                    base.evaluate()
                } finally {
                    Airplane.set(false)
                }
            }
        }
}

internal object Airplane {
    private val inst get() = InstrumentationRegistry.getInstrumentation()

    fun shell(cmd: String) = Shell.run(cmd)

    fun isOn(): Boolean = Settings.Global.getInt(
        inst.targetContext.contentResolver, "airplane_mode_on", 0) == 1

    /**
     * 出网探测：能建立 TCP 连接才算"真的有网"（不看系统标志自说自话）。
     *
     * 目标必须选**本环境真的可达**的：实测 1.1.1.1:443 / 8.8.8.8:443 在本网络被整体
     * 封锁（TCP 连不上），只拿它们判断会把"有网"误判成"没网"（第一次运行就踩到：
     * 恢复网络后探测仍报 false）。任一目标连上即算有网（走 DNS 也算：断网时域名也解析不了）。
     */
    private val PROBE_TARGETS = listOf(
        "223.5.5.5" to 443,          // 国内可达，IP 字面量不走 DNS
        "api.mangadex.org" to 443,   // 真实源站（设备上实测可用）
        "1.1.1.1" to 443,
    )

    fun outboundOk(timeoutMs: Int = 3000): Boolean = PROBE_TARGETS.any { (host, port) ->
        try {
            Socket().use { it.connect(InetSocketAddress(host, port), timeoutMs) }
            true
        } catch (t: Throwable) {
            false
        }
    }

    /** 切换并等待**探测结果**与目标一致（返回是否达成） */
    fun set(on: Boolean, timeoutMs: Long = 40_000): Boolean {
        shell("cmd connectivity airplane-mode ${if (on) "enable" else "disable"}")
        val t0 = System.currentTimeMillis()
        while (System.currentTimeMillis() - t0 < timeoutMs) {
            if (isOn() == on && outboundOk() != on) return true
            Thread.sleep(400)
        }
        return false
    }
}

@RunWith(AndroidJUnit4::class)
@LargeTest
class NetworkLossTest {

    @get:Rule(order = 0)
    val airplaneRule = AirplaneRule()

    @get:Rule(order = 1)
    val rule = createAndroidComposeRule<MainActivity>()

    private lateinit var comic: SelfTestComic
    private lateinit var gateway: EngineGateway

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext

    private fun ev(line: String) = println("NETWORK_LOSS_EVIDENCE $line")

    private fun texts(text: String, substring: Boolean = false) =
        rule.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().size

    private fun nodes(tag: String) = rule.onAllNodesWithTag(tag).fetchSemanticsNodes().size

    private fun waitText(text: String, timeoutMs: Long = 150_000, substring: Boolean = false) {
        rule.waitUntil(timeoutMs) { texts(text, substring) > 0 }
    }

    private fun clickText(text: String) {
        waitText(text)
        rule.onAllNodesWithText(text)[0].performClick()
    }

    /** 读出带 testTag 节点的全部文本（用于打印证据，避免"测试说通过、界面说的却是别的"） */
    private fun tagText(tag: String): String {
        val ns = rule.onAllNodesWithTag(tag).fetchSemanticsNodes()
        return ns.joinToString(" | ") { n ->
            n.config.getOrNull(SemanticsProperties.Text)?.joinToString("") { it.text } ?: ""
        }
    }

    @Before
    fun setUp() {
        comic = SelfTestComic()
        comic.create()
        gateway = EngineGateway(ctx)
        ev("已造自检漫画（本地文件，不依赖网络）")
    }

    @After
    fun tearDown() {
        // 双保险：任何情况下都不能把设备留在断网状态
        Airplane.set(false)
        comic.cleanup()
        ev("清理完成：网络已恢复=${Airplane.outboundOk()}")
    }

    // ── 1) 断网冷启动：本机服务不受影响，也不许出现桌面网络提示 ─────────────

    @Test
    @OfflineAtStart
    fun offlineColdStart_engineStillWorks_andNoDesktopNetworkHint() {
        assertTrue("用例前提：启动时设备必须真的断网（否则此用例无意义）",
            !Airplane.outboundOk())
        waitText("书架", timeoutMs = 150_000)
        rule.waitUntil(180_000) {
            texts("继续阅读") > 0 || texts("书架还是空的") > 0 ||
                texts("漫画（", true) > 0 || texts("小说（", true) > 0 ||
                texts("本机引擎未就绪") > 0
        }
        assertTrue("断网冷启动不得以「引擎未就绪」收场：引擎是本机回环服务，与外网无关",
            texts("本机引擎未就绪") == 0)
        assertTrue("断网时不得出现桌面网络提示（回环失效/更换主机地址/访问电脑）",
            texts("更换主机", true) == 0 && texts("回环", true) == 0 &&
                texts("电脑", true) == 0 && texts("服务未就绪", true) == 0)
        assertTrue("四个页签必须在位",
            texts("浏览") > 0 && texts("下载") > 0 && texts("设置") > 0)
        ev("断网冷启动：引擎可用、页签在位、无桌面网络提示")
    }

    // ── 2) 断网读已下载漫画（§8 D：断网重启后已下载内容可打开）─────────────

    @Test
    @OfflineAtStart
    fun offlineReadsDownloadedComic_fromLocalFiles() {
        assertTrue("用例前提：必须真的断网", !Airplane.outboundOk())
        waitText(SelfTestComic.TITLE, timeoutMs = 180_000)
        ev("断网状态下书架仍列出已下载漫画 ${SelfTestComic.TITLE}")

        clickText(SelfTestComic.TITLE)
        waitText("开始阅读", timeoutMs = 60_000)
        clickText("开始阅读")
        waitText("1 / 3 · 1/2页", timeoutMs = 60_000)
        rule.onNodeWithTag("manga_pages").assertExists()
        assertTrue("断网阅读不得出现图片加载失败占位",
            texts("页加载失败", true) == 0)
        ev("断网阅读：第 1 话 2 页来自本地文件，无失败占位")
    }

    // ── 3) 断网搜索：如实说"本机没网"，且恢复网络后不用重启即可继续 ─────────

    @Test
    fun offlineSearch_saysNetworkIsDown_andRecoversWithoutRestart() {
        waitText("书架", timeoutMs = 150_000)
        rule.onAllNodesWithText("浏览")[0].performClick()
        waitText("漫画（优先）", timeoutMs = 60_000)
        rule.onAllNodesWithText("搜漫画")[0].performClick()
        waitText("漫画搜索", timeoutMs = 30_000)
        ev("已进入原生漫画搜索页（此时有网）")

        assertTrue("切换断网失败（设备仍有网），用例无法继续", Airplane.set(true))
        ev("已断网：出网探测=${Airplane.outboundOk()}")

        // 关键词刻意选"不可能有结果"的：服务端只缓存非空结果，所以这条搜索
        // **不会命中缓存**——否则离线时看到的是上次缓存的结果，就测不到"断网归因"
        // （实测踩到：设备上"巨人"已被别的用例缓存，离线也照样出结果）。
        rule.onNodeWithTag("manga_search_field").performTextInput("zzz断网自检关键词")
        val t0 = System.currentTimeMillis()
        rule.onNodeWithTag("manga_search_btn").performClick()

        // 必须有**确定终态**：结果 / 明确失败说明 / 逐源说明，任一出现即算收尾
        rule.waitUntil(90_000) {
            nodes("manga_search_results") > 0 || nodes("manga_search_errors") > 0 ||
                nodes("manga_search_offline") > 0 ||
                texts("没有结果") > 0 || texts("搜索失败", true) > 0
        }
        // A) 首屏原因：搜索**进行中**就应该能看到逐源失败原因（本次新增的活体显示），
        //    不用等整轮收尾——断网时源站请求几十毫秒内就失败
        val msFirst = System.currentTimeMillis() - t0
        val liveText = tagText("manga_search_errors_live")
        ev("断网搜索首屏原因 ${msFirst}ms（搜索进行中即可见）=「${liveText.take(160)}」")

        // B) 等整轮真正收尾：按钮从"搜索中…"回到"搜索"，并给出结论块
        rule.waitUntil(60_000) {
            texts("搜索中…") == 0 &&
                (nodes("manga_search_offline") > 0 || texts("没有结果") > 0)
        }
        val ms = System.currentTimeMillis() - t0
        // 服务端在生产代码里用"设备级证据（X-Device-Net: offline / 直连探测）"判定本机没网，
        // 并在 _MANGA_OFFLINE_GRACE(3s) 后**提前收尾**。修复前实测要等满 25s
        // （禁漫/拷贝拖到自己的源站期限），这里锁住"不许再等源站期限"。
        assertTrue("断网搜索必须在数秒内给终态（提前收尾），实际 ${ms}ms", ms < 20_000)
        val errText = tagText("manga_search_errors")
        ev("断网搜索终态 ${ms}ms（服务端提前收尾）；逐源说明=「${errText.take(200)}」")

        // 证据：再直接读一遍同一条流，打印**引擎侧**耗时与逐源结果，
        // 用来区分"引擎慢"还是"界面慢"（第一次运行时界面 25s 才出终态，
        // 而源在 1s 内就全失败了——必须查清是谁在等）
        runBlocking {
            val ep = gateway.currentEndpoint()
                ?: (gateway.connect() as? EngineState.Ready)?.endpoint
            if (ep != null) {
                var finElapsed = -1.0
                var done = -1
                var total = -1
                var netDown = false
                var nErr = 0
                val t2 = System.currentTimeMillis()
                gateway.streamEvents(ep.port,
                    "/api/manga/search/stream?q=" + java.net.URLEncoder.encode("巨人", "UTF-8")) { e ->
                    if (e.optBoolean("finished")) {
                        finElapsed = e.optDouble("elapsed", -1.0)
                        done = e.optInt("done"); total = e.optInt("total")
                        netDown = e.optBoolean("network_down")
                        nErr = e.optJSONObject("errors")?.length() ?: 0
                    }
                }
                ev("引擎侧同一搜索：墙钟 ${System.currentTimeMillis() - t2}ms，" +
                    "服务端 elapsed=${finElapsed}s，done=$done/$total，逐源失败=$nErr，" +
                    "network_down=$netDown")
            }
        }

        // 诚实性：断网时不能把原因说成"源站异常或限流"
        assertTrue("断网时必须指出是本机网络问题，而不是源站问题；实际逐源说明=「$errText」",
            errText.contains("网络") || errText.contains("本机"))
        ev("断网搜索如实归因：${if (errText.contains("本机")) "本机网络" else "网络"}")

        // 结论口径：必须说"本机当前没有网络"，不许把"没网"说成"没有结果"（错的归因）
        assertTrue("断网且整轮无结果时，界面必须给出「本机当前没有网络」而不是「没有结果」",
            nodes("manga_search_offline") > 0)
        ev("断网终态口径：本机当前没有网络（不是「没有结果」）")

        // 恢复网络：不重启、不重进页面，直接重试即可继续用。
        // 断言口径：**"本机没有网络"这个结论必须消失**，搜索回到正常语义
        // （有结果，或"没有结果"——关键词是刻意选的空结果词）。
        assertTrue("恢复网络失败", Airplane.set(false))
        ev("网络已恢复：出网探测=${Airplane.outboundOk()}")
        val t1 = System.currentTimeMillis()
        rule.onAllNodesWithText("重试")[0].performClick()
        rule.waitUntil(120_000) {
            texts("搜索中…") == 0 &&
                (nodes("manga_search_results") > 0 || texts("没有结果") > 0)
        }
        val ms2 = System.currentTimeMillis() - t1
        assertEquals("恢复网络后不得再声称本机没网", 0, nodes("manga_search_offline"))
        ev("恢复网络后重试：${ms2}ms 回到正常搜索语义（未重启引擎），" +
            "结果=${nodes("manga_search_results") > 0}，无结果提示=${texts("没有结果") > 0}")
    }
}
