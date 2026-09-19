package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.security.MessageDigest

/**
 * 内置源**跨版本升级合并**验收（方向基线 §6.4；2026-09-15 评审 P1-内置源）。
 *
 * 评审指出的实际界限：
 *   · 清单只固定 schema=1，同 schema 直接返回 → 以后**只改策略或源规则**时，
 *     已安装设备永远不会重新合并；
 *   · 旧实现只写 `enabled`，不写 URL/规则正文 → "规则升级"等于没做；
 *   · 未被 ours 记录的**旧版本出厂文件**，内容变化时会被误判成"用户改过"而跳过；
 *   · 原有用例改的是标记里的 schema，对同一套资产重跑，**没有**覆盖真实的新旧版本差异。
 *
 * 本用例用**注入的清单 + 注入的出厂内容**造出"真·跨版本"场景，逐条锁住：
 *   1. 旧出厂内容（哈希在出厂历史里）→ 必须被升级为新内容；
 *   2. 用户改过的文件 → 一个字都不许动；
 *   3. 策略变化（默认停用）→ 对出厂文件生效；
 *   4. schema 不变、revision 变化 → 仍然重新合并；
 *   5. 同 schema + 同 revision → 幂等（0 改动）；
 *   6. 目标不可写 → 不崩、不写坏文件（如实记日志，下次启动再试）。
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class SeedUpgradeTest {

    private val ctx get() = InstrumentationRegistry.getInstrumentation().targetContext

    private fun ev(line: String) = println("SEED_UPGRADE_EVIDENCE $line")

    private fun sha(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes)
            .joinToString("") { "%02x".format(it) }

    private fun freshDir(): File {
        val d = File(ctx.cacheDir, "seed-upgrade-sources")
        d.deleteRecursively()
        d.mkdirs()
        return d
    }

    /** 造一份假清单：出厂内容历史里带上 oldHash，当前内容哈希是 newHash */
    private fun fakeManifest(name: String, newHash: String, oldHash: String?,
                             enabledByDefault: Boolean, revision: String,
                             schema: Int = 1): String { 
        val factory = JSONArray()
        if (oldHash != null) factory.put(oldHash)
        factory.put(newHash)
        return JSONObject()
            .put("schema", schema)
            .put("revision", revision)
            .put("source_count", 1)
            .put("enabled_by_default", if (enabledByDefault) 1 else 0)
            .put("factory_hashes", JSONObject().put(name, factory))
            .put("sources", JSONArray().put(JSONObject()
                .put("uid", "seed.up.uid").put("name", name).put("file", name)
                .put("sha256", newHash).put("enabled_by_default", enabledByDefault)
                .put("reason", "")))
            .toString()
    }

    private fun source(rule: String, enabled: Boolean = true): ByteArray =
        (JSONObject().put("uid", "seed.up.uid")
            .put("bookSourceName", "跨版本源")
            .put("searchUrl", rule)
            .put("enabled", enabled).toString()).toByteArray(Charsets.UTF_8)

    @Test
    fun crossVersionMerge_upgradesRules_keepsUserEdits_andIsIdempotent() {
        val d = freshDir()
        val name = "seed-upgrade.json"
        val device = File(d, name)

        // ── 设备当前状态（模拟"装过旧版本 + 用户自己动过另一个文件"）──
        val oldRule = "/old/search?q={{key}}"                  // 旧出厂内容
        val newRule = "/new/search?q={{key}}&v=2"              // 新版本出厂内容
        device.writeBytes(source(oldRule, enabled = true))     // 旧出厂文件（未被 ours 记录）
        val oldHash = sha(device.readBytes())

        // 用户改过的文件：内容与任何出厂版本都不一致
        val userName = "user-edited.json"
        val userFile = File(d, userName)
        userFile.writeBytes(source("/mine/search?q={{key}}", enabled = false))
        val userHash = sha(userFile.readBytes())

        // 策略：这个源在出厂清单里默认启用
        val manifest = fakeManifest(name, sha(source(newRule, enabled = true)),
            oldHash, enabledByDefault = true, revision = "rev-v2")

        // ── 1) 旧出厂内容必须被升级为新内容（这才是"规则升级"）──
        val changed = BundledSources.applySeed(ctx, d, manifestText = manifest,
            assetReader = { n -> if (n == name) source(newRule, enabled = true) else null })
        ev("跨版本合并：改动 $changed 个")
        val afterText = device.readText(Charsets.UTF_8)
        assertTrue("旧出厂文件必须被升级为新规则（实际：$afterText）",
            afterText.contains("v=2"))
        val marker = JSONObject(File(d, ".seed-applied").readText(Charsets.UTF_8))
        assertEquals("内容升级应计入 content_updated", 1, marker.optInt("content_updated"))
        assertEquals("升级后的哈希应记进 ours",
            sha(device.readBytes()), marker.optJSONObject("ours")!!.optString(name))
        ev("规则正文已升级：${oldRule} → ${newRule}（ours 已记录新哈希）")

        // ── 2) 用户改过的文件一个字都不许动 ──
        assertEquals("用户文件不得被改动", userHash, sha(userFile.readBytes()))
        assertEquals("应如实记下跳过的用户文件", 1, marker.optInt("modified_skipped"))
        ev("用户改过的文件保持原样（modified_skipped=1）")

        // ── 3) 幂等：同 schema + 同 revision 再跑 → 0 改动 ──
        assertEquals("同 revision 重跑必须 0 改动", 0,
            BundledSources.applySeed(ctx, d, manifestText = manifest,
                assetReader = { n -> if (n == name) source(newRule, enabled = true) else null }))
        ev("同 schema + 同 revision 重跑 0 改动（幂等）")

        // ── 4) schema 不变、只有 revision 变 → 必须重新合并 ──
        //    场景：策略变化（这个源改成默认停用），内容不变
        val manifestV3 = fakeManifest(name, sha(source(newRule, enabled = true)),
            oldHash, enabledByDefault = false, revision = "rev-v3")
        val changed2 = BundledSources.applySeed(ctx, d, manifestText = manifestV3,
            assetReader = { n -> if (n == name) source(newRule, enabled = true) else null })
        assertEquals("策略变化必须生效（revision 变了，schema 没变）", 1, changed2)
        assertTrue("清单说默认停用 → 必须真的停用",
            !JSONObject(device.readText(Charsets.UTF_8)).optBoolean("enabled", true))
        assertEquals("这次只改启用状态，没改内容", 0,
            JSONObject(File(d, ".seed-applied").readText(Charsets.UTF_8))
                .optInt("content_updated"))
        ev("revision 变化触发重新合并：仅策略变化也生效（enabled→false）")

        // ── 5) 用户改过的文件即使 revision 再变也不碰 ──
        val manifestV4 = fakeManifest(name, sha(source(newRule, enabled = true)),
            oldHash, enabledByDefault = true, revision = "rev-v4")
        BundledSources.applySeed(ctx, d, manifestText = manifestV4,
            assetReader = { n -> if (n == name) source(newRule, enabled = true) else null })
        assertEquals("用户文件始终不得被改动", userHash, sha(userFile.readBytes()))
        ev("revision 再变：用户文件依旧保持原样")

        d.deleteRecursively()
    }

    @Test
    fun unwritableTarget_doesNotCrashOrCorrupt() {
        val d = freshDir()
        val name = "seed-ro.json"
        val device = File(d, name)
        device.writeBytes(source("/old/search", enabled = true))
        val manifest = fakeManifest(name, sha(source("/new/search", enabled = true)),
            sha(device.readBytes()), enabledByDefault = true, revision = "rev-ro")
        val before = device.readBytes()
        try {
            val locked = device.setWritable(false)
            val changed = BundledSources.applySeed(ctx, d, manifestText = manifest,
                assetReader = { device.readBytes() })
            val after = device.readBytes()
            // 1) 不许崩：能返回就说明没抛出去（异常被记日志并返回 0/计数）
            ev("只读目标：合并返回 $changed（未抛异常）；setWritable=$locked")
            // 2) 不许写坏：内容要么没变，要么变成新内容，绝不能是半截
            if (after.contentEquals(before)) {
                ev("文件保持原样（写失败被如实吞掉，下次启动再试）")
            } else {
                assertTrue("写了就必须是完整的新内容",
                    String(after, Charsets.UTF_8).contains("/new/search"))
                ev("该设备仍允许写：内容已是新版本且完整")
            }
            assertTrue("文件必须仍是合法 JSON",
                runCatching { JSONObject(String(after, Charsets.UTF_8)) }.isSuccess)
        } finally {
            device.setWritable(true)
            d.deleteRecursively()
        }
    }

    @Test
    fun legacyInstallWithLostOursMemory_isReclaimed_andThenUpgrades() {
        // 现场实测（emulator-5554）：某次安装后 .seed-applied 里的 ours 为空，
        // 24 个源的文件内容是"出厂内容 + 种子按策略改过的 enabled"，
        // 于是全被判成"用户改过"→ 规则升级永远到不了设备。
        // 本用例锁住修复：这种残留必须先被认回出厂、恢复记忆，之后才谈得上升级。
        val d = freshDir()
        val name = "seed-legacy.json"
        val device = File(d, name)

        val v1 = "/v1/search?q={{key}}"
        val v2 = "/v2/search?q={{key}}"
        val assetV1 = source(v1, enabled = false)     // 出厂文件（桌面状态：停用）
        val assetV2 = source(v2, enabled = false)     // 新版出厂文件
        // 设备现状：出厂内容 + 我们按策略改过的 enabled=true；ours 为空、无历史哈希
        device.writeBytes(source(v1, enabled = true))

        // 1) 认回出厂：除 enabled 外与出厂内容一致，且 enabled 正好是策略值 → 可更新
        val m1 = fakeManifest(name, sha(assetV1), oldHash = null,
            enabledByDefault = true, revision = "rev-legacy-1")
        val n1 = BundledSources.applySeed(ctx, d, manifestText = m1,
            assetReader = { n -> if (n == name) assetV1 else null })
        val marker1 = JSONObject(File(d, ".seed-applied").readText(Charsets.UTF_8))
        ev("旧安装残留：改动 $n1 个，modified_skipped=${marker1.optInt("modified_skipped")}")
        assertEquals("出厂内容（仅 enabled 被种子改过）不得被当成用户改动",
            0, marker1.optInt("modified_skipped"))

        // 2) 记忆恢复：种子把当前内容记进 ours
        assertEquals("应把当前内容记进 ours",
            sha(device.readBytes()), marker1.optJSONObject("ours")!!.optString(name))

        // 3) 真正的规则升级：新版出厂文件必须能落到这个设备上
        val m2 = fakeManifest(name, sha(assetV2), oldHash = sha(assetV1),
            enabledByDefault = true, revision = "rev-legacy-2")
        val n2 = BundledSources.applySeed(ctx, d, manifestText = m2,
            assetReader = { n -> if (n == name) assetV2 else null })
        val text = device.readText(Charsets.UTF_8)
        ev("规则升级：改动 $n2 个，文件现在=「${text.take(80)}」")
        // 注意：org.json 的 toString 会把 / 转义成 \/ —— 断言走 JSON 取值，不做字符串包含
        assertEquals("旧出厂文件必须被升级为新规则", "/v2/search?q={{key}}",
            JSONObject(text).optString("searchUrl"))
        assertTrue("升级后策略仍生效（enabled=true）",
            JSONObject(text).optBoolean("enabled", false))
        d.deleteRecursively()
    }
}
