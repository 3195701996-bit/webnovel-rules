package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * 书源管理契约验收（不联网、不改动用户真实书源）：
 *
 *   - GET /api/sources 能列出书源，且"未校验"不会被当成"通过"（valid 缺省 = null）；
 *   - 用**自造的一个书源文件**做 启用 → 停用 → 删除 的完整往返（落在设备真实 sources 目录，
 *     结束删掉，不留残留）；
 *   - 导入接口对不合规地址必须拒绝并给出原因（SSRF 防护），且**没有落盘**；
 *   - 缺少 bookSourceUrl 的导入必须报错而不是"成功 0 个"含糊过去。
 */
@RunWith(AndroidJUnit4::class)
class SourcePathTest {

    private lateinit var gateway: EngineGateway
    private val ctx = InstrumentationRegistry.getInstrumentation().targetContext
    private val sourcesDir = File(ctx.filesDir, "runtime/sources")
    private val uid = "__selftest_source__"
    private val srcFile = File(sourcesDir, "$uid.json")

    private fun ev(line: String) = println("SOURCE_PATH_EVIDENCE $line")

    @Before
    fun setUp() {
        gateway = EngineGateway(ctx)
        val state = runBlocking { gateway.connect() }
        assertTrue("引擎未就绪：$state", state is EngineState.Ready)
    }

    @After
    fun tearDown() {
        runCatching { runBlocking { gateway.stopEngine() } }
        srcFile.delete()
        assertFalse("自检书源未清理干净", srcFile.exists())
        // 顺带确认导入尝试没有留下任何以自检地址命名的书源
        sourcesDir.listFiles { f -> f.name.contains("selftest") }?.forEach { it.delete() }
        assertEquals("sources 目录里仍有自检残留", 0,
            sourcesDir.listFiles { f -> f.name.contains("selftest") }?.size ?: 0)
    }

    private fun writeSource(enabled: Boolean, valid: Boolean?) {
        sourcesDir.mkdirs()
        val o = JSONObject().apply {
            put("uid", uid)
            put("bookSourceName", "自检书源")
            put("bookSourceUrl", "https://selftest.example.com")
            put("bookSourceGroup", "自检")
            put("enabled", enabled)
            if (valid != null) put("valid", valid) else put("valid", JSONObject.NULL)
            put("searchUrl", "/search?q={{key}}")
        }
        srcFile.writeText(o.toString(), Charsets.UTF_8)
    }

    @Test
    fun bookSources_listToggleDeleteImport() = runBlocking {
        val ep = gateway.currentEndpoint()!!

        // 1) 先造一个未校验且启用的书源
        writeSource(enabled = true, valid = null)

        val list = gateway.httpText(ep.port, "/api/sources")
        assertTrue("书源接口状态码=${list.code}", list.ok)
        val all = EngineData.bookSources(list.body)
        assertTrue("书源列表不应为空", all.isNotEmpty())
        val mine = all.firstOrNull { it.uid == uid }
        assertNotNull("列表里没有自检书源", mine)
        assertEquals("名称", "自检书源", mine!!.name)
        assertTrue("应为启用", mine.enabled)
        assertNull("未校验必须解析成 null，而不是 false/true 冒充结果", mine.valid)
        ev("列表：共 ${all.size} 个，自检书源启用=${mine.enabled} 校验=${mine.valid ?: "未校验"}")

        // 2) 停用 → 列表反映停用
        val off = gateway.httpPost(ep.port, "/api/sources/$uid/toggle",
            JSONObject().put("enabled", false).toString())
        assertTrue("停用应成功：HTTP ${off.code}", off.ok)
        assertEquals(false, JSONObject(off.body).optBoolean("enabled"))
        val afterOff = EngineData.bookSources(gateway.httpText(ep.port, "/api/sources").body)
            .firstOrNull { it.uid == uid }
        assertEquals("停用后应为 disabled", false, afterOff?.enabled)
        ev("停用后：enabled=${afterOff?.enabled}")

        // 3) 再启用
        val on = gateway.httpPost(ep.port, "/api/sources/$uid/toggle",
            JSONObject().put("enabled", true).toString())
        assertTrue("启用应成功：HTTP ${on.code}", on.ok)
        val afterOn = EngineData.bookSources(gateway.httpText(ep.port, "/api/sources").body)
            .firstOrNull { it.uid == uid }
        assertEquals("启用后应为 enabled", true, afterOn?.enabled)
        ev("再启用后：enabled=${afterOn?.enabled}")

        // 4) 删除 → 列表里消失、文件也没了
        val del = gateway.httpDelete(ep.port, "/api/sources/$uid")
        assertTrue("删除应成功：HTTP ${del.code}", del.ok)
        val afterDel = EngineData.bookSources(gateway.httpText(ep.port, "/api/sources").body)
        assertTrue("删除后列表不应再含自检书源", afterDel.none { it.uid == uid })
        assertFalse("删除后书源文件应被移除", srcFile.exists())
        ev("删除后：列表已无自检书源，文件存在=${srcFile.exists()}")

        // 5) 导入：不合规地址（.invalid 不可能是公网）必须被拒并给出原因
        val bad = gateway.httpPost(ep.port, "/api/sources/import",
            JSONObject().put("content", JSONObject().apply {
                put("bookSourceName", "自检-内网源")
                put("bookSourceUrl", "http://127.0.0.1:8766")
            }.toString()).toString())
        assertTrue("导入请求本身应 200（逐源结果在 results 里）：HTTP ${bad.code}", bad.ok)
        val badObj = JSONObject(bad.body)
        assertEquals("不合规地址不应计入导入成功", 0, badObj.optInt("imported"))
        val results = badObj.optJSONArray("results")
        assertTrue("应有逐源结果", (results?.length() ?: 0) > 0)
        val first = results!!.optJSONObject(0)
        assertEquals("该源应标记失败", false, first.optBoolean("ok"))
        assertTrue("应给出拒绝原因，实际=${first.optString("error")}",
            first.optString("error").isNotBlank())
        assertTrue("sources 目录里不应出现该导入文件",
            sourcesDir.listFiles { f -> f.name.contains("selftest") }?.isEmpty() != false)
        ev("导入内网地址被拒：imported=0 原因=${first.optString("error").take(40)}")

        // 6) 导入：缺 bookSourceUrl 也必须明确失败
        val malformed = gateway.httpPost(ep.port, "/api/sources/import",
            JSONObject().put("content", JSONObject().put("bookSourceName", "自检-缺地址").toString())
                .toString())
        val malObj = JSONObject(malformed.body)
        assertEquals("缺地址不应计入成功", 0, malObj.optInt("imported"))
        assertTrue("应给出错误说明",
            malObj.optJSONArray("results")?.optJSONObject(0)?.optString("error")?.isNotBlank() == true)
        ev("导入缺地址被拒：${malObj.optJSONArray("results")?.optJSONObject(0)?.optString("error")}")

        // 7) 非法 JSON 文本：必须 400，而不是静默成功
        val garbage = gateway.httpPost(ep.port, "/api/sources/import",
            JSONObject().put("content", "{这不是 JSON").toString())
        assertFalse("非法 JSON 不应返回 2xx", garbage.ok)
        ev("导入非法 JSON：HTTP ${garbage.code}")
    }
}
