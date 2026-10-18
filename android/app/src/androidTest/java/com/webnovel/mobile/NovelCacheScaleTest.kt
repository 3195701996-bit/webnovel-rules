package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.io.File
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/** Real embedded Python/cache API, isolated synthetic novel; never touches a user book. */
@RunWith(AndroidJUnit4::class)
class NovelCacheScaleTest {
    @Test
    fun largeNovelValidatesCachesAndInvalidatesRepairs() = runBlocking {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val key = "selftest_novel_cache_scale_${System.nanoTime()}"
        val directory = File(context.filesDir, "runtime/books/$key")
        val gateway = EngineGateway(context)
        val endpoint = (gateway.connect() as? EngineState.Ready)?.endpoint
            ?: error("本地引擎未就绪")
        check(directory.mkdirs())
        try {
            val chapters = JSONArray()
            val completed = JSONArray()
            val body = "这是离线自检正文。".repeat(200)
            for (index in 1..5035) {
                val url = "https://selftest.invalid/cache-scale/chapter-$index"
                chapters.put(JSONObject().put("name", "第${index}章").put("url", url))
                completed.put(url)
                File(directory, SelfTestBook.cacheKeyOf(url) + ".cache").writeText(body)
            }
            File(directory, "_state.json").writeText(JSONObject()
                .put("book", JSONObject().put("name", "缓存性能自检").put("source_uid", "selftest"))
                .put("chapters", chapters).put("completed", completed)
                .put("failed", JSONObject()).toString())

            suspend fun detail(): Pair<JSONObject, Long> {
                val started = android.os.SystemClock.elapsedRealtime()
                val response = gateway.httpText(endpoint.port, "/api/books/$key")
                assertTrue("详情接口失败 ${response.code}", response.ok)
                return JSONObject(response.body) to (android.os.SystemClock.elapsedRealtime() - started)
            }
            val (cold, coldMs) = detail()
            val (warm, warmMs) = detail()
            assertEquals(5035, cold.getInt("done"))
            assertEquals(5035, warm.getInt("done"))
            val first = File(directory, SelfTestBook.cacheKeyOf(chapters.getJSONObject(0).getString("url")) + ".cache")
            first.writeBytes(byteArrayOf(0xff.toByte(), 0xfe.toByte()))
            assertEquals(5034, detail().first.getInt("done"))
            first.writeText(body)
            assertEquals(5035, detail().first.getInt("done"))
            println("NOVEL_CACHE_SCALE chapters=5035 cold_ms=$coldMs warm_ms=$warmMs api=${android.os.Build.VERSION.SDK_INT}")
            assertTrue("首次详情超过15秒：$coldMs", coldMs < 15_000)
            assertTrue("重复详情超过5秒：$warmMs", warmMs < 5_000)
        } finally {
            directory.deleteRecursively()
            assertTrue("隔离书目未清理", !directory.exists())
        }
    }
}
