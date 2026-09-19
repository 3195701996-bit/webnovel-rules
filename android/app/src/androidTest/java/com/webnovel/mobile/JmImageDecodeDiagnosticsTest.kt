package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * **禁漫图片在设备上为什么读不出来**——只诊断、不断言（结果打进 logcat）。
 *
 * 起因：用户反馈"禁漫加载过慢"，实测剖析时发现图片端点全部返回 409 且服务端日志是
 * `[jm] 图片还原失败: cannot identify image file` —— 也就是 **Pillow 解不开取回的字节**。
 * 桌面（macOS 版 Pillow）能解开同样的图，所以怀疑是 **APK 里的 Pillow 缺少 WebP 支持**
 * （禁漫图片 URL 多为 .webp）。
 *
 * 本用例在设备上直接问 Chaquopy 的 Python：
 *   1. `PIL.features.check('webp')` / `('jpg')` —— Pillow 到底支不支持
 *   2. 真取一张禁漫图片：HTTP 状态、Content-Type、前 16 字节（判断是不是图片）
 *   3. `PIL.Image.open()` 是否能打开（用真实字节，不用合成图）
 * 只打印证据，不写失败断言（源站不可达时也不该让套件红）。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class JmImageDecodeDiagnosticsTest {

    private fun ev(line: String) = println("JM_DECODE_EVIDENCE $line")

    @Test
    fun reportPillowFeaturesAndRealJmImageBytes() {
        val ctx = InstrumentationRegistry.getInstrumentation().targetContext
        val gw = EngineGateway(ctx)
        val st = kotlinx.coroutines.runBlocking { gw.connect() }
        if (st !is EngineState.Ready) { ev("引擎未就绪，跳过"); return }
        val port = st.endpoint.port

        // 1) Pillow 能力（Chaquopy 里的那份）
        try {
            val py = com.chaquo.python.Python.getInstance()
            // 直接用一段 Python 代码问，避免 Kotlin→Python 的调用细节干扰结论
            val probe = py.getModule("builtins").callAttr(
                "exec",
                "import PIL, PIL.features as F\n" +
                    "print('PIL', getattr(PIL, '__version__', '?'))\n" +
                    "for f in ('webp','jpg','zlib','libjpeg_turbo'):\n" +
                    "    try:\n" +
                    "        print('feature', f, F.check(f))\n" +
                    "    except Exception as e:\n" +
                    "        print('feature', f, 'ERR', e)\n",
                py.getModule("builtins").callAttr("dict"))
        } catch (t: Throwable) {
            ev("问 Pillow 能力失败：${t.javaClass.simpleName}: ${t.message}")
        }

        // 1b) 逐步验证 Android 解码器互操作（定位 Chaquopy 调 Java 的哪一步出问题）
        try {
            val py = com.chaquo.python.Python.getInstance()
            py.getModule("builtins").callAttr("exec",
                "from java import jclass\n" +
                "BF = jclass('android.graphics.BitmapFactory')\n" +
                "Bitmap = jclass('android.graphics.Bitmap')\n" +
                "print('STEP1 jclass ok')\n" +
                "print('STEP2 Config.ARGB_8888 =', Bitmap.Config.ARGB_8888)\n" +
                "bmp = Bitmap.createBitmap(4, 4, Bitmap.Config.ARGB_8888)\n" +
                "print('STEP3 createBitmap ok', bmp.getWidth(), bmp.getHeight())\n" +
                "px = [0] * (4 * 4)\n" +
                "bmp.getPixels(px, 0, 4, 0, 0, 4, 4)\n" +
                "print('STEP4 getPixels ok', px[:4])\n" +
                "bmp2 = Bitmap.createBitmap(4, 4, Bitmap.Config.ARGB_8888)\n" +
                "bmp2.setPixels(px, 0, 4, 0, 0, 4, 4)\n" +
                "print('STEP5 setPixels ok')\n" +
                "from java.io import ByteArrayOutputStream\n" +
                "buf = ByteArrayOutputStream()\n" +
                "print('STEP6 compress =', bmp2.compress(Bitmap.CompressFormat.JPEG, 90, buf))\n" +
                "print('STEP7 bytes =', len(bytes(buf.toByteArray())))\n",
                py.getModule("builtins").callAttr("dict"))
        } catch (t: Throwable) {
            ev("互操作探测失败：${t.javaClass.simpleName}: ${t.message}")
        }

        // 2) 取一部 jm 作品 → 一章 → 一张真图
        var cid = ""
        var chid = ""
        val br = kotlinx.coroutines.runBlocking { gw.httpText(port, "/api/manga/browse?source=jm&page=1") }
        runCatching {
            val arr = JSONObject(br.body).optJSONArray("results")!!
            cid = arr.optJSONObject(0)!!.optString("id")
        }
        if (cid.isBlank()) { ev("jm 排行拿不到作品，跳过后续"); return }
        val dr = kotlinx.coroutines.runBlocking { gw.httpText(port, "/api/manga/jm/$cid") }
        runCatching {
            chid = JSONObject(dr.body).optJSONArray("chapters")!!.optJSONObject(0)!!.optString("id")
        }
        ev("目标：jm/$cid 第 $chid 话")
        if (chid.isBlank()) return

        val t0 = System.nanoTime()
        val cr = kotlinx.coroutines.runBlocking { gw.httpText(port, "/api/manga/jm/$cid/chapter/$chid") }
        ev("图列表 HTTP ${cr.code}  ${(System.nanoTime() - t0) / 1_000_000}ms")
        var firstUrl = ""
        runCatching {
            firstUrl = JSONObject(cr.body).optJSONArray("images")!!.optString(0)
        }
        ev("首图 URL：${firstUrl.take(120)}")

        // 3) 在设备上直接取这张图（绕过我们的下载器，看原始字节）
        if (firstUrl.isNotBlank()) {
            try {
                val py = com.chaquo.python.Python.getInstance()
                val req = py.getModule("requests")
                val ad = py.getModule("engine.manga.manager").callAttr("get_adapter", "jm", null)
                val hdrJson = JSONObject()
                for ((k, v) in ad.callAttr("image_headers", firstUrl).asMap()) {
                    hdrJson.put(k.toString(), v.toString())
                }
                val pyDict = py.getModule("json").callAttr("loads", hdrJson.toString())
                val r = req.callAttr("get", firstUrl, pyDict, 30000)
                val code = r.callAttr("status_code").toInt()
                val ct = r.callAttr("headers").callAttr("get", "Content-Type").toString()
                val content = r.callAttr("content").toJava(ByteArray::class.java)
                ev("裸取首图：HTTP $code  Content-Type=$ct  字节=${content.size}")
                ev("前 16 字节：${content.take(16).joinToString(" ") { "%02x".format(it) }}")
                val img = py.getModule("PIL.Image")
                try {
                    val im = img.callAttr("open", py.getModule("io").callAttr("BytesIO", content))
                        .callAttr("convert", "RGB")
                    ev("PIL 打开成功：size=${im.callAttr("size")}  格式=${im.callAttr("format") ?: "n/a"}")
                } catch (t: Throwable) {
                    ev("PIL 打开失败：${t.javaClass.simpleName}: ${t.message}")
                }
            } catch (t: Throwable) {
                ev("裸取首图失败：${t.javaClass.simpleName}: ${t.message}")
            }
        }

        // 4) 走我们自己的端点：0.63.0 起应当**成功取到图**（Android 解码器兜底 WebP）
        val t1 = System.nanoTime()
        val ir = kotlinx.coroutines.runBlocking { gw.httpText(port, "/api/manga/jm/$cid/chapter/$chid/img/0") }
        val dt = (System.nanoTime() - t1) / 1_000_000
        ev("本机 /img/0：HTTP ${ir.code}  ${dt}ms  body=${ir.body.take(160)}")
        assertTrue("0.63.0 起禁漫图片必须能在设备上取到（WebP 由 Android 解码器兜底）：" +
            "HTTP ${ir.code} ${ir.body.take(200)}", ir.code == 200)

        // 5) 连取 8 张：全部应为 200（不再 409），并给出耗时分布
        var ok = 0
        var total = 0L
        for (i in 0 until 8) {
            val t = System.nanoTime()
            val r = kotlinx.coroutines.runBlocking { gw.httpText(port, "/api/manga/jm/$cid/chapter/$chid/img/$i") }
            total += (System.nanoTime() - t) / 1_000_000
            if (r.code == 200) ok++ else ev("  #$i HTTP ${r.code} ${r.body.take(80)}")
        }
        ev("连取 8 张：成功 $ok/8，共 ${total}ms（平均 ${total / 8}ms）")
        assertTrue("连取应有绝大多数成功（实际 $ok/8）", ok >= 6)
        Unit
    }
}
