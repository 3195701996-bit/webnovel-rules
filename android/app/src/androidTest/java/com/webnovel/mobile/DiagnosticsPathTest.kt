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
 * 脱敏诊断报告真机验收（方向基线 §7.1：用户无需先成为测试人员）
 *
 * 目的：用户手机上报"漫画完全不可用"时，需要**一次导出**就能定位卡在哪一层。
 * 这条用例验证报告在真机上：分节齐全、失败请求可追溯、**不夹带凭据**。
 */
@RunWith(AndroidJUnit4::class)
class DiagnosticsPathTest {

    private lateinit var gateway: EngineGateway

    private fun ev(line: String) = println("DIAGNOSTICS_EVIDENCE $line")

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

    private suspend fun report(port: Int): String {
        val r = gateway.httpText(port, "/api/diagnostics/report")
        assertTrue("诊断报告应 200：HTTP ${r.code}", r.ok)
        val text = runCatching { JSONObject(r.body).optString("text") }.getOrDefault("")
        assertTrue("报告正文不能为空", text.isNotBlank())
        return text
    }

    @Test
    fun report_isUsableAndSanitized() = runBlocking {
        val ep = gateway.currentEndpoint()!!

        // 1) 先制造一条可追溯的失败请求
        gateway.httpText(ep.port, "/api/manga/browse?source=nhentai&q=私人搜索词")

        val text = report(ep.port)
        ev("报告字节数=${text.toByteArray().size}")

        // 2) 分节齐全（缺一节就等于分层定位断了一环）
        for (section in listOf("【1. 引擎】", "【2. 能力台账", "【3. 书源",
                               "【4. 漫画源", "【5. 存储】",
                               "【6. 最近的失败请求与异常】", "【7. 本报告的范围与脱敏声明】")) {
            assertTrue("报告缺少小节：$section", text.contains(section))
        }
        // 3) 手机运行时状态必须在（否则用户设备上根本看不出引擎是否起来）
        assertTrue("报告应包含手机运行时信息",
            text.contains("state：") || text.contains("不是手机 App 运行时"))
        // 4) 漫画源分层：依赖判定与实测结论分开，且未实测就说未实测
        assertTrue("漫画源节应列出适配器与实测结论",
            text.contains("依赖判定=") && text.contains("实测="))
        // 5) 失败请求可追溯，且查询串被剥掉（不夹带私人搜索词）
        assertTrue("应记录到刚才的失败请求", text.contains("/api/manga/browse"))
        assertFalse("查询串必须剥掉", text.contains("私人搜索词"))
        ev("分节齐全；失败请求已记录且查询串已剥除")

        // 6) 脱敏：报告里不得出现本机会话凭据
        val token = ep.token
        assertTrue("会话凭据不能出现在报告里", token.isBlank() || !text.contains(token))
        assertFalse("报告不得包含 Cookie 字样内容", text.contains("mobile_session="))
        ev("脱敏检查通过（凭据/会话值未出现）")

        // 7) 事件接口与报告口径一致
        val evr = gateway.httpText(ep.port, "/api/diagnostics/events")
        assertTrue("事件接口应 200", evr.ok)
        ev("事件接口可用：HTTP ${evr.code}")
    }
}
