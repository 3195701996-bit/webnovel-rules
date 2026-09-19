package com.webnovel.mobile

import android.content.Context
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * 内置书源的解压（APK 的 assets/sources → 应用私有数据目录）。
 *
 * 为什么单独抽出来：书源随 APK 一起分发，但**只有引擎启动时才解压**——
 * 于是"刚装好、还没启动过引擎"时去备份，备份里就没有书源（实测踩到：
 * 全新安装后跑备份用例，备份只有 36→0 个书源文件）。
 * 现在服务启动与备份都会先调 [ensure]，行为一致且幂等。
 */
object BundledSources {

    private const val TAG = "BundledSources"
    const val ASSET_DIR = "sources"

    /** 删除墓碑文件名（用户删掉/清理掉的源：**不许**在下次启动时又解压回来） */
    private const val TOMBSTONE = ".seed-removed.json"

    private fun removedNames(dst: File): Set<String> {
        val f = File(dst, TOMBSTONE)
        if (!f.isFile) return emptySet()
        return runCatching {
            JSONObject(f.readText(Charsets.UTF_8))
                .optJSONObject("files")?.keys()?.asSequence()?.toSet() ?: emptySet()
        }.getOrDefault(emptySet())
    }

    /** 确保 [dst] 里有内置书源；返回本次新解出的数量（已存在则为 0，用户删掉的不再加回）。 */
    fun ensure(ctx: Context, dst: File): Int {
        var n = 0
        try {
            if (!dst.exists() && !dst.mkdirs()) {
                Log.w(TAG, "无法创建书源目录：$dst")
                return 0
            }
            val removed = removedNames(dst)
            var skipped = 0
            val names = ctx.assets.list(ASSET_DIR) ?: return 0
            for (name in names) {
                if (!name.endsWith(".json")) continue
                // 用户删过/清理过的源：尊重用户选择，不再解压回来
                // （实测：在 App 里清掉 4 个重复文件，下次启动又被解出 4 个——"删了又回来"）
                if (name in removed) { skipped++; continue }
                val out = File(dst, name)
                if (out.exists()) continue
                ctx.assets.open("$ASSET_DIR/$name").use { input ->
                    java.io.FileOutputStream(out).use { output -> input.copyTo(output) }
                }
                n++
            }
            if (skipped > 0) {
                Log.i(TAG, "内置书源解出：新增 $n 个；按删除墓碑跳过 $skipped 个" +
                    "（用户删掉的源不会被加回来）")
            }
        } catch (t: Throwable) {
            Log.w(TAG, "解出内置书源失败: ${t.javaClass.simpleName}: ${t.message}")
        }
        return n
    }

    /** 当前已有多少个书源文件 */
    fun count(dst: File): Int =
        dst.listFiles { f -> f.isFile && f.name.endsWith(".json") }?.size ?: 0

    const val SEED_MANIFEST = "mobile-sources.json"
    private const val SEED_MARKER = ".seed-applied"

    /**
     * **合并逻辑版本**：判定规则（怎么认出"这是出厂文件"）本身也会演进，
     * 而 schema/revision 只描述清单，不描述判定规则。
     *
     * 为什么要它（实测教训）：0.47.0 的判定规则在某些设备上把 24 个源误判成
     * "用户改过"，标记里却按同一 revision 写了"已应用"——于是即便修好了判定规则，
     * 那台设备也会因为 schema+revision 都不变而**永远不再重扫**，24 个源继续冻结。
     * 逻辑版本变化时强制重扫一次；重扫不改内容就不会写文件（幂等仍然成立）。
     *
     * 2 = 增加"出厂内容 + 我们改过的 enabled"识别（sameIgnoringEnabled）与
     *     ours 记忆恢复（见 applySeed 的四个证据）。
     */
    private const val SEED_LOGIC = 2

    /**
     * 按**内置源种子清单**把设备上的书源对齐到出厂状态（方向基线 §6.4：
     * 清单 + 稳定 ID + 配置版本 + **升级合并策略**，且不覆盖用户自定义状态）。
     *
     * 背景：APK 直接复制桌面 sources 目录，会把"该时点桌面的启用状态"一起带入——
     * 实测桌面 34 个源里 28 个停用，手机端一装只有 6 个源可用，首启体验不稳定。
     *
     * 2026-09-15 评审修正（P1-内置源）之后的口径：
     *   · **清单格式 schema 与内容 revision 分开**：任一源文件内容或源集合变化都会
     *     改变 revision，即使 schema 不变也会重新合并——旧实现只看 schema，
     *     结果"以后只改策略/改规则"永远到不了已安装的设备；
     *   · **真的写内容**，不只写 enabled：出厂文件（规则/URL/正文）会被更新为当前
     *     出厂内容；旧实现只改 enabled，规则升级等于没做；
     *   · 判定"这是不是出厂文件"有三个证据：当前清单哈希、我们上次写入后记录的
     *     哈希（.seed-applied 的 ours）、以及**出厂历史哈希**（清单里的
     *     factory_hashes，由构建期 seed-history.json 累积）——第三个专门用来
     *     认出"旧版本出厂文件"，避免把它误判成用户改动而永久跳过；
     *   · 只要三个证据都不成立 → 认定用户改过 → **整个文件跳过**，绝不动。
     *
     * @param manifestText 清单 JSON（默认读 APK assets；测试可注入假清单）
     * @param assetReader 取出厂文件内容（默认读 APK assets；测试可注入）
     * @return 实际改动的源数量
     */
    fun applySeed(ctx: Context, dst: File,
                  manifestText: String? = null,
                  assetReader: ((String) -> ByteArray?)? = null): Int {
        var changed = 0
        try {
            val text = manifestText ?: ctx.assets.open(SEED_MANIFEST).use {
                it.readBytes().toString(Charsets.UTF_8)
            }
            val readAsset: (String) -> ByteArray? = assetReader ?: { name ->
                runCatching {
                    ctx.assets.open("$ASSET_DIR/$name").use { it.readBytes() }
                }.getOrNull()
            }
            val root = JSONObject(text)
            val schema = root.optInt("schema", 0)
            val revision = root.optString("revision")
            val marker = File(dst, SEED_MARKER)
            val applied = runCatching {
                if (marker.exists()) JSONObject(marker.readText(Charsets.UTF_8)) else JSONObject()
            }.getOrDefault(JSONObject())
            // 幂等：schema 与 revision 都没变才算"已应用过"；
            // revision 为空（旧清单）时退回只看 schema 的老行为
            val sameSchema = applied.optInt("schema", -1) == schema
            val sameRev = revision.isBlank() || applied.optString("revision") == revision
            val sameLogic = applied.optInt("logic", 0) == SEED_LOGIC
            if (sameSchema && sameRev && sameLogic) return 0
            // 上一次由**我们**改过的文件（文件名 → 改后 sha256）：策略升级时要能把它们
            // 再对齐，而不是误判成用户改过就永久放弃
            val ours = applied.optJSONObject("ours") ?: JSONObject()
            val factory = root.optJSONObject("factory_hashes") ?: JSONObject()

            val arr = root.optJSONArray("sources") ?: return 0
            // 按 uid 索引：文件名不一定等于 uid（本项目就有这种源）
            val byUid = HashMap<String, JSONObject>()
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                val uid = o.optString("uid")
                if (uid.isNotBlank()) byUid[uid] = o
            }
            val files = dst.listFiles { f -> f.isFile && f.name.endsWith(".json") } ?: return 0
            var untouched = 0
            var modified = 0
            var contentUpdated = 0
            var enabledChanged = 0
            var memoryRecorded = 0
            for (f in files) {
                val bytes = f.readBytes()
                val o = runCatching { JSONObject(String(bytes, Charsets.UTF_8)) }.getOrNull()
                    ?: continue
                val uid = o.optString("uid").ifBlank { f.name.removeSuffix(".json") }
                val meta = byUid[uid] ?: continue
                val hash = sha256(bytes)
                val want = meta.optBoolean("enabled_by_default", true)
                val isOurs = hash == ours.optString(f.name)
                val isFactoryHistory = factoryHashesOf(factory, f.name).contains(hash)
                val assetName = meta.optString("file").ifBlank { f.name }
                val assetBytes = readAsset(assetName)
                val assetObj = assetBytes?.let {
                    runCatching { JSONObject(String(it, Charsets.UTF_8)) }.getOrNull()
                }
                // 第四个证据（2026-09-15 评审补充）：设备文件是**出厂内容，只是 enabled
                // 被种子按策略改过**，而且当时没记进 ours（旧版本安装、标记曾丢过）。
                // 判据：除 enabled 外与出厂内容完全一致，且 enabled 正好等于清单策略值
                // ——说明这个文件处在出厂状态，没有留下任何用户特有内容可保护。
                // 用户自己改过规则/URL/其它字段 → 不满足；用户把 enabled 改成别的值 →
                // 也不满足（那条记录会被当成用户选择保留下来）。
                val isFactoryModuloEnabled = assetObj != null &&
                    sameIgnoringEnabled(o, assetObj) && o.optBoolean("enabled", true) == want
                if (hash != meta.optString("sha256") && !isOurs && !isFactoryHistory &&
                    !isFactoryModuloEnabled) {
                    modified++          // 用户改过 → 不碰
                    continue
                }
                untouched++
                var touched = false
                // 1) 出厂**内容**（规则/URL/正文）：与出厂不一致才写（除 enabled 外）
                if (assetBytes != null && assetObj != null && !sameIgnoringEnabled(o, assetObj)) {
                    f.writeBytes(assetBytes)
                    contentUpdated++
                    touched = true
                }
                // 2) 出厂**启用状态**按清单策略（资产里的 enabled 是桌面状态，不做准）
                val cur = runCatching {
                    JSONObject(f.readText(Charsets.UTF_8)).optBoolean("enabled", true)
                }.getOrDefault(true)
                if (cur != want) {
                    val jo = runCatching { JSONObject(f.readText(Charsets.UTF_8)) }.getOrNull()
                    if (jo != null) {
                        jo.put("enabled", want)
                        f.writeText(jo.toString(), Charsets.UTF_8)
                        enabledChanged++
                        touched = true
                    }
                }
                // 3) **恢复记忆**：只要确认它是出厂文件，就把当前内容哈希记进 ours——
                //    即使这次什么都没写。否则"出厂内容 + 种子改过的 enabled"这类旧安装
                //    残留永远对不上清单哈希，下一版规则升级又会被误判成用户改动而跳过
                //    （实测：某设备 ours 为空 → 24 个源被冻结）。
                val nowHash = sha256(f.readBytes())
                if (ours.optString(f.name) != nowHash) {
                    ours.put(f.name, nowHash)
                    memoryRecorded++
                }
                if (touched) changed++
            }
            marker.writeText(
                JSONObject().put("schema", schema)
                    .put("revision", revision)
                    .put("logic", SEED_LOGIC)
                    .put("applied_at", System.currentTimeMillis())
                    .put("changed", changed)
                    .put("content_updated", contentUpdated)
                    .put("enabled_changed", enabledChanged)
                    .put("untouched", untouched)
                    .put("modified_skipped", modified)
                    .put("memory_recorded", memoryRecorded)
                    .put("ours", ours).toString(), Charsets.UTF_8)
            Log.i(TAG, "内置源种子合并：schema=$schema revision=${revision.take(12)} " +
                "logic=$SEED_LOGIC 改动 $changed 个" +
                "（内容 $contentUpdated、启用 $enabledChanged；出厂未改动 $untouched，" +
                "用户改过而跳过 $modified，记忆 $memoryRecorded）")
        } catch (t: Throwable) {
            Log.w(TAG, "内置源种子合并失败（不影响书源可用）: " +
                "${t.javaClass.simpleName}: ${t.message}")
        }
        return changed
    }

    /**
     * 除 `enabled` 外两个 JSON 是否完全一致（顺序无关，递归比较）。
     *
     * 用途见 applySeed 的第四个证据：认出"出厂内容 + 种子改过的 enabled"这种旧安装
     * 残留，避免把**我们自己的写入**误判成用户改动而永久跳过（实测：某设备标记里的
     * ours 为空，24 个源因此被当成"用户改过"，规则升级全部到不了设备）。
     */
    private fun sameIgnoringEnabled(a: JSONObject, b: JSONObject): Boolean {
        val ka = a.keys().asSequence().filter { it != "enabled" }.toSortedSet()
        val kb = b.keys().asSequence().filter { it != "enabled" }.toSortedSet()
        if (ka != kb) return false
        for (k in ka) if (!jsonValueEquals(a.opt(k), b.opt(k))) return false
        return true
    }

    private fun jsonValueEquals(a: Any?, b: Any?): Boolean = when {
        a is JSONObject && b is JSONObject -> sameIgnoringEnabled(a, b)
        a is JSONArray && b is JSONArray ->
            a.length() == b.length() && (0 until a.length()).all {
                jsonValueEquals(a.opt(it), b.opt(it))
            }
        a == null || b == null -> a == b
        else -> a == b || a.toString() == b.toString()
    }

    /** 出厂历史哈希（清单里的 factory_hashes[文件名] 数组） */
    private fun factoryHashesOf(factory: JSONObject, name: String): Set<String> {
        val arr = factory.optJSONArray(name) ?: return emptySet()
        val out = HashSet<String>()
        for (i in 0 until arr.length()) {
            val v = arr.optString(i)
            if (v.isNotBlank()) out.add(v)
        }
        return out
    }

    /** 清单/标记的可读摘要（给诊断报告与界面用，不猜） */
    fun seedSummary(ctx: Context, dst: File): String {
        val marker = File(dst, SEED_MARKER)
        val applied = runCatching {
            if (marker.exists()) JSONObject(marker.readText(Charsets.UTF_8)) else null
        }.getOrNull()
        val m = runCatching {
            JSONObject(ctx.assets.open(SEED_MANIFEST).use {
                it.readBytes().toString(Charsets.UTF_8)
            })
        }.getOrNull()
        return buildString {
            append("清单 schema=").append(m?.optInt("schema") ?: -1)
            append(" · revision ").append((m?.optString("revision") ?: "").take(12))
            append("，源 ").append(m?.optInt("source_count") ?: -1)
            append("（默认启用 ").append(m?.optInt("enabled_by_default") ?: -1).append("）")
            if (applied == null) {
                append("；尚未合并到本机")
            } else {
                append("；已合并 revision ").append(applied.optString("revision").take(12))
                append("：改动 ").append(applied.optInt("changed"))
                append("（内容 ").append(applied.optInt("content_updated"))
                append("、启用 ").append(applied.optInt("enabled_changed")).append("）")
                append("，出厂未改动 ").append(applied.optInt("untouched"))
                append("，用户改过而跳过 ").append(applied.optInt("modified_skipped"))
                append("，记忆 ").append(applied.optInt("memory_recorded"))
            }
        }
    }

    private fun sha256(bytes: ByteArray): String =
        java.security.MessageDigest.getInstance("SHA-256").digest(bytes)
            .joinToString("") { "%02x".format(it) }
}
