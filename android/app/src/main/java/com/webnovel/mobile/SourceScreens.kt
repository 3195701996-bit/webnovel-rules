@file:OptIn(ExperimentalMaterial3Api::class)

package com.webnovel.mobile

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import org.json.JSONObject

// ── 暖墨暗色：本屏共用的两个小样式 ────────────────────────────

/** 状态筛选 Chip：WnChipShape，选中态 accent 填充、未选 line 描边 + muted 文案 */
@Composable
private fun WnFilterChip(
    selected: Boolean,
    onClick: () -> Unit,
    label: String,
    modifier: Modifier = Modifier,
) {
    FilterChip(
        selected = selected,
        onClick = onClick,
        label = { Text(label, maxLines = 1) },
        shape = WnChipShape,
        colors = FilterChipDefaults.filterChipColors(
            containerColor = WnColors.surface,
            labelColor = WnColors.inkDim,
            selectedContainerColor = WnColors.accent,
            selectedLabelColor = WnColors.onAccent,
        ),
        border = FilterChipDefaults.filterChipBorder(
            enabled = true,
            selected = selected,
            borderColor = WnColors.line,
            selectedBorderColor = WnColors.accent,
        ),
        modifier = modifier,
    )
}

/**
 * 三轴状态小徽标（pill）：未验证 = inkDim 描边、通过 = ok 填充、失败 = danger 描边。
 * 用 Text 直接承载（而不是容器包裹），保证 testTag 节点的 semantics 里带完整文案。
 */
@Composable
private fun WnStatusPill(
    text: String,
    color: Color,
    filled: Boolean,
    modifier: Modifier = Modifier,
) {
    Text(
        text,
        style = MaterialTheme.typography.labelSmall,
        color = if (filled) WnColors.onAccent else color,
        maxLines = 2,
        overflow = TextOverflow.Ellipsis,
        modifier = modifier
            .background(if (filled) color else Color.Transparent, WnPillShape)
            .border(1.dp, color, WnPillShape)
            .padding(horizontal = WnSpace.sm, vertical = WnSpace.xs),
    )
}

/** 提示卡统一外壳：surface 底 + 1dp 发丝边框（颜色按语义给），无阴影 */
@Composable
private fun WnHintCard(
    borderColor: Color,
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit,
) {
    Card(
        modifier = modifier.fillMaxWidth(),
        shape = WnCardShape,
        colors = CardDefaults.cardColors(containerColor = WnColors.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
        border = BorderStroke(1.dp, borderColor),
    ) {
        Column(Modifier.padding(WnSpace.md)) { content() }
    }
}

// ── 书源管理 ─────────────────────────────────────────────────

/**
 * 书源管理（原生）：启用/停用、逐源校验、删除、从文件导入。
 *
 * 与网页端同一份 sources 目录下的 .json（本机引擎读同一个目录），
 * 因此这里改完，网页端与 App 看到的是同一状态；不复制第二份配置。
 *
 * 诚实标注：每个源显示"是否启用 / 上次校验结果 / 校验时间"，
 * 未校验过的显示"未校验"，不冒充已通过。
 */
@Composable
fun BookSourceScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    onBack: () -> Unit,
) {
    val ctx = androidx.compose.ui.platform.LocalContext.current
    var sources by remember { mutableStateOf<List<BookSource>>(emptyList()) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var msg by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var verifs by remember { mutableStateOf<Map<String, SourceVerification>>(emptyMap()) }
    var verifyMsg by remember { mutableStateOf<String?>(null) }
    var verifyMeta by remember { mutableStateOf("" to "") }
    var verifySummary by remember { mutableStateOf<EngineData.VerifySummary?>(null) }
    // 筛选：源多的时候（内置 34 个）必须能按状态看，否则"哪些该启用"无从下手
    var filter by remember { mutableStateOf("all") }
    var bulkBusy by remember { mutableStateOf(false) }
    // 重复源文件（同一 uid/同站地址的两份文件）：检测结论 + 是否在工作 + 对话框开关
    var dupReport by remember { mutableStateOf<org.json.JSONObject?>(null) }
    var dupBusy by remember { mutableStateOf(false) }
    var dupDialog by remember { mutableStateOf(false) }
    var dupResult by remember { mutableStateOf<String?>(null) }
    // 被清理/删除的源文件（备份可恢复）：数量 + 是否在恢复中 + 恢复结果
    var removedCount by remember { mutableStateOf(0) }
    // 最近一批的数量：一键恢复**一次只恢复一批**（界面承诺的是"最近一次清理/删除"，
    // 服务端也从 0.74.27 起按 latest 只恢复最近一批）。按钮上必须写这一批的数量，
    // 否则按钮写"（9 个）"、点完只回来 3 个，用户会以为又坏了一次。
    var removedLatest by remember { mutableStateOf(0) }
    var restoreBusy by remember { mutableStateOf(false) }
    var restoreDialog by remember { mutableStateOf(false) }
    var restoreResult by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    suspend fun reload() {
        val r = gateway.httpText(ep.port, "/api/sources")
        val list = if (r.ok) EngineData.bookSources(r.body) else null
        if (list == null) {
            error = "读取书源失败：HTTP ${r.code}"
        } else {
            sources = list
            error = null
        }
        // 重复源文件（同一 uid 的两份文件）——给出可执行结论，而不是让用户去用电脑
        val dr = gateway.httpText(ep.port, "/api/sources/duplicates")
        dupReport = if (dr.ok) runCatching { org.json.JSONObject(dr.body) }.getOrNull() else null
        // 被清理/删除的源文件（备份可恢复）——清理动作只移动不删除，备份在应用私有目录里，
        // 用户点不到；这里给出"可恢复 N 个"与一键恢复，避免"清错了找不回来"
        val rr = gateway.httpText(ep.port, "/api/sources/removed")
        removedCount = if (rr.ok) {
            runCatching { org.json.JSONObject(rr.body).optInt("total") }.getOrDefault(0)
        } else 0
        removedLatest = if (rr.ok) {
            runCatching { org.json.JSONObject(rr.body).optInt("latest_count") }.getOrDefault(0)
        } else 0
        // 功能验证结果单独取：没有结果时就是"未验证"，不冒充通过
        val vr = gateway.httpText(ep.port, "/api/sources/verify/results")
        if (vr.ok) {
            verifs = EngineData.verifications(vr.body).associateBy { it.uid }
            verifyMeta = EngineData.verifyMeta(vr.body)
            verifySummary = EngineData.verifySummary(vr.body)
        }
        loading = false
    }
    LaunchedEffect(ep) { reload() }

    /**
     * 清理重复源文件：把"同一 uid 的两份文件"里多余的那份**移到备份目录**（不是删除）。
     * 只提交服务端**已判定为可安全清理**的那些文件名；内容不一致的组不会被提交，
     * 服务端也会再校验一次（不允许删任意文件）。
     */
    fun cleanupDuplicates() {
        val report = dupReport ?: return
        val groups = report.optJSONArray("groups") ?: return
        val files = ArrayList<String>()
        for (i in 0 until groups.length()) {
            val g = groups.optJSONObject(i) ?: continue
            if (!g.optBoolean("safe")) continue
            val arr = g.optJSONArray("remove") ?: continue
            for (j in 0 until arr.length()) files.add(arr.optString(j))
        }
        if (files.isEmpty()) {
            dupDialog = false
            dupResult = "没有可安全清理的重复文件"
            return
        }
        scope.launch {
            dupBusy = true
            dupResult = null
            val body = org.json.JSONObject().put("files", org.json.JSONArray(files)).toString()
            val r = gateway.httpPost(ep.port, "/api/sources/duplicates/cleanup", body)
            val o = runCatching { org.json.JSONObject(r.body) }.getOrNull()
            dupResult = if (!r.ok) {
                "清理失败：HTTP ${r.code} " +
                    (o?.optString("error")?.take(100) ?: r.body.take(100))
            } else {
                val moved = o?.optJSONArray("moved")?.length() ?: 0
                val refused = o?.optJSONArray("refused")?.length() ?: 0
                buildString {
                    append("已清理 ").append(moved).append(" 个重复文件（移到备份目录，可恢复）")
                    if (refused > 0) append("；").append(refused).append(" 个被拒绝（需人工确认）")
                    append("。备份：").append(o?.optString("backup_dir") ?: "")
                }
            }
            dupBusy = false
            dupDialog = false
            reload()
        }
    }

    /** 恢复被清理/删除的源文件（不覆盖已有同名文件；恢复后解除删除墓碑） */
    fun restoreRemoved() {
        scope.launch {
            restoreBusy = true
            restoreResult = null
            val r = gateway.httpPost(ep.port, "/api/sources/removed/restore",
                org.json.JSONObject().put("latest", true).toString())
            val o = runCatching { org.json.JSONObject(r.body) }.getOrNull()
            restoreResult = if (!r.ok) {
                "恢复失败：HTTP ${r.code} " +
                    (o?.optString("error")?.take(100) ?: r.body.take(100))
            } else {
                val n = o?.optJSONArray("restored")?.length() ?: 0
                val skip = o?.optJSONArray("skipped")?.length() ?: 0
                val left = o?.optInt("remaining") ?: 0
                buildString {
                    append("已恢复 ").append(n).append(" 个源文件")
                    if (skip > 0) append("；").append(skip).append(" 个跳过（书源目录已有同名文件，不覆盖）")
                    // 还有更早批次就说清楚"再点一次可以继续"，不然用户以为只恢复了这些
                    if (left > 0) append("。还有 ").append(left).append(" 个在更早的批次里，再点一次可继续恢复")
                }
            }
            restoreBusy = false
            restoreDialog = false
            reload()
        }
    }

    /**
     * 批量启用/停用（按验证结论筛选）。
     * 只按**已记录的**验证结论筛；没有记录的源不会被"验证通过"带上（没测过 ≠ 通过）。
     * 回显服务端给的真实条数，不写"操作成功"这种没有信息量的话。
     */
    fun bulkEnabled(enabled: Boolean, filterKey: String, label: String) {
        scope.launch {
            bulkBusy = true
            msg = null
            val body = JSONObject().put("enabled", enabled)
                .put("filter", filterKey).toString()
            val r = gateway.httpPost(ep.port, "/api/sources/bulk-enabled", body)
            val o = runCatching { JSONObject(r.body) }.getOrNull()
            msg = if (!r.ok) {
                "批量操作失败：HTTP ${r.code} " +
                    (o?.optString("error")?.take(80) ?: r.body.take(80))
            } else {
                buildString {
                    // 两种口径分开说：唯一源数 与 文件数（同 uid 的重复文件会让二者不等）
                    append(label).append("：改动 ").append(o?.optInt("changed") ?: 0)
                        .append(" 个文件（匹配 ").append(o?.optInt("matched") ?: 0)
                        .append(" 个源 / ").append(o?.optInt("matched_entries") ?: 0)
                        .append(" 个文件，共 ").append(o?.optInt("total") ?: 0)
                        .append(" 个唯一源 / ").append(o?.optInt("total_entries") ?: 0)
                        .append(" 个文件）")
                    append("；当前启用 ").append(o?.optInt("enabled_now") ?: 0)
                        .append(" 个源（").append(o?.optInt("enabled_entries") ?: 0)
                        .append(" 个文件）")
                    val note = o?.optString("note") ?: ""
                    if (note.isNotBlank()) append(" · ").append(note)
                }
            }
            bulkBusy = false
            reload()
        }
    }

    // 导出书源到用户选定的文件（SAF）：请求体由服务端生成，客户端只负责搬运字节
    val exportLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.CreateDocument("application/json")
    ) { uri: Uri? ->
        if (uri == null) {
            msg = "已取消导出"
            return@rememberLauncherForActivityResult
        }
        scope.launch {
            busy = true; msg = "正在导出…"
            msg = try {
                val r = Export.streamToUri(ctx, ep.port, ep.token, "/api/sources/export", uri)
                "已导出 ${sources.size} 个书源（${Export.human(r.bytes)}）。" +
                    "该文件可原样导入回来，也可发给别人。"
            } catch (t: Throwable) {
                "导出失败：${t.message ?: t.javaClass.simpleName}"
            }
            busy = false
        }
    }

    // 从设备里选一个书源 .json 导入（Legado 格式，由服务端校验与落盘）
    val importLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri: Uri? ->
        if (uri == null) {
            msg = "已取消导入"
            return@rememberLauncherForActivityResult
        }
        scope.launch {
            busy = true
            msg = try {
                val text = ctx.contentResolver.openInputStream(uri)?.use {
                    it.readBytes().toString(Charsets.UTF_8)
                } ?: throw IllegalStateException("无法读取所选文件")
                // 服务端既接受数组/对象，也接受 {"content": "<json 文本>"}
                val body = JSONObject().put("content", text).toString()
                val r = gateway.httpPost(ep.port, "/api/sources/import", body)
                val o = runCatching { JSONObject(r.body) }.getOrNull()
                if (!r.ok) {
                    "导入失败：HTTP ${r.code} ${r.body.take(120)}"
                } else {
                    val n = o?.optInt("imported") ?: 0
                    val results = o?.optJSONArray("results")
                    val failed = (0 until (results?.length() ?: 0)).count { i ->
                        results?.optJSONObject(i)?.optBoolean("ok") != true
                    }
                    buildString {
                        append("导入完成：成功 $n 个")
                        if (failed > 0) append("，被拒绝 $failed 个（含校验失败/地址不合规）")
                        // 首个失败原因如实展示，避免"导入失败但不说为什么"
                        for (i in 0 until (results?.length() ?: 0)) {
                            val it = results?.optJSONObject(i) ?: continue
                            if (!it.optBoolean("ok")) {
                                append("；原因：").append(it.optString("error").take(80))
                                break
                            }
                        }
                    }
                }
            } catch (t: Throwable) {
                "导入失败：${t.message ?: t.javaClass.simpleName}"
            }
            busy = false
            reload()
        }
    }

    // 筛选口径与批量操作口径一致：都按 uid 的验证结论（列表与摘要必须一致，
    // 否则"筛选后 3 个"却列出 5 张卡片）
    val vcount: (String) -> Int = { st -> verifs.values.count { it.status == st } }
    val enabledN = sources.count { it.enabled }
    val shown = sources.filter { s ->
        val st = verifs[s.uid]?.status
        when (filter) {
            "enabled" -> s.enabled
            "disabled" -> !s.enabled
            "verified" -> st == "verified"
            "failed" -> st == "failed"
            "unverified" -> st == null
            else -> true
        }
    }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text("书源管理", maxLines = 1) },
            navigationIcon = { TextButton(onClick = onBack) { Text("← 返回") } },
            actions = {
                TextButton(onClick = { importLauncher.launch(arrayOf("application/json", "*/*")) },
                    enabled = !busy) { Text(if (busy) "处理中…" else "导入") }
                // 导出全部书源（P1-2「SAF 导入导出」的导出侧）：用户自己选保存位置，
                // 文件就是书源 JSON，能直接看、能发给别人、也能原样导回
                TextButton(
                    onClick = { exportLauncher.launch(Export.safeFileName("书源导出.json", "sources.json")) },
                    enabled = !busy && sources.isNotEmpty(),
                    modifier = Modifier.testTag("src_export_all"),
                ) { Text("导出") }
            },
        )
    }) { pad ->
        Box(Modifier.padding(pad).fillMaxSize()) {
            when {
                loading -> Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
                error != null -> Column(Modifier.fillMaxSize().padding(24.dp), Arrangement.Center) {
                    Text(error!!, color = MaterialTheme.colorScheme.error)
                    Spacer(Modifier.height(12.dp))
                    Button(onClick = { scope.launch { reload() } }) { Text("重试") }
                }
                else -> LazyColumn(Modifier.fillMaxSize().testTag("book_sources"),
                                   contentPadding = PaddingValues(12.dp),
                                   verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    item {
                        val dup = sources.groupBy { it.uid }.filterValues { it.size > 1 }
                        // 顶部统计摘要：标题带总数，计数明细用 muted 一行
                        WnSectionHeader(title = "书源", count = sources.size)
                        Text(
                            "启用 $enabledN 个" +
                                (if (filter == "all") "" else "；筛选后 ${shown.size} 个") +
                                "；配置与网页端共用同一份文件。",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        Spacer(Modifier.height(WnSpace.sm))
                        // 状态筛选轴：源多时按状态看（全部/启用/停用/验证通过/失败/未验证）
                        Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                            horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                            listOf(
                                "all" to "全部 ${sources.size}",
                                "enabled" to "已启用 $enabledN",
                                "disabled" to "已停用 ${sources.size - enabledN}",
                                "verified" to "验证通过 ${vcount("verified")}",
                                "failed" to "验证失败 ${vcount("failed")}",
                                "unverified" to "未验证 ${sources.count { verifs[it.uid] == null }}",
                            ).forEach { (key, label) ->
                                WnFilterChip(
                                    selected = filter == key,
                                    onClick = { filter = key },
                                    label = label,
                                    modifier = Modifier.testTag("src_filter_$key"),
                                )
                            }
                        }
                        Spacer(Modifier.height(WnSpace.sm))
                        // 批量操作收成一行小按钮：一次点完 34 个源的启用状态
                        //（这正是手机端最缺的能力）；四个操作一个不少
                        Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                            horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                            OutlinedButton(
                                enabled = !bulkBusy,
                                onClick = { bulkEnabled(true, "verified", "启用验证通过的源") },
                                shape = WnPillShape,
                                contentPadding = PaddingValues(horizontal = 10.dp, vertical = 2.dp),
                                border = BorderStroke(1.dp, WnColors.accent),
                                colors = ButtonDefaults.outlinedButtonColors(
                                    contentColor = WnColors.accent),
                                modifier = Modifier.testTag("src_bulk_enable_verified"),
                            ) { Text("启用验证通过的（${vcount("verified")}）",
                                     style = MaterialTheme.typography.labelSmall) }
                            OutlinedButton(
                                enabled = !bulkBusy,
                                onClick = { bulkEnabled(false, "failed", "停用验证失败的源") },
                                shape = WnPillShape,
                                contentPadding = PaddingValues(horizontal = 10.dp, vertical = 2.dp),
                                border = BorderStroke(1.dp, WnColors.danger),
                                colors = ButtonDefaults.outlinedButtonColors(
                                    contentColor = WnColors.danger),
                                modifier = Modifier.testTag("src_bulk_disable_failed"),
                            ) { Text("停用验证失败的（${vcount("failed")}）",
                                     style = MaterialTheme.typography.labelSmall) }
                            OutlinedButton(
                                enabled = !bulkBusy,
                                onClick = { bulkEnabled(true, "all", "启用全部源") },
                                shape = WnPillShape,
                                contentPadding = PaddingValues(horizontal = 10.dp, vertical = 2.dp),
                                modifier = Modifier.testTag("src_bulk_enable_all"),
                            ) { Text("全部启用（${sources.size}）",
                                     style = MaterialTheme.typography.labelSmall) }
                            OutlinedButton(
                                enabled = !bulkBusy,
                                onClick = { bulkEnabled(false, "all", "停用全部源") },
                                shape = WnPillShape,
                                contentPadding = PaddingValues(horizontal = 10.dp, vertical = 2.dp),
                                border = BorderStroke(1.dp, WnColors.danger),
                                colors = ButtonDefaults.outlinedButtonColors(
                                    contentColor = WnColors.danger),
                                modifier = Modifier.testTag("src_bulk_disable_all"),
                            ) { Text("全部停用", style = MaterialTheme.typography.labelSmall) }
                        }
                        val dupSafe = dupReport?.optInt("safe_groups") ?: 0
                        val dupReview = dupReport?.optInt("review_groups") ?: 0
                        val dupRemovable = dupReport?.optInt("removable") ?: 0
                        if (dup.isNotEmpty() || dupSafe > 0 || dupReview > 0) {
                            Spacer(Modifier.height(WnSpace.sm))
                            WnHintCard(borderColor = WnColors.danger,
                                modifier = Modifier.testTag("src_dup_card")) {
                                Text("重复源文件：$dupSafe 组可安全清理" +
                                    (if (dupReview > 0) "，$dupReview 组内容不同需人工确认" else ""),
                                    style = MaterialTheme.typography.labelMedium,
                                    fontWeight = FontWeight.Medium,
                                    color = MaterialTheme.colorScheme.error)
                                Spacer(Modifier.height(2.dp))
                                Text("同一 uid 的两份文件会让「启用/删除」只作用于其中一个。" +
                                    "清理只把多余那份**移到备份目录**（可恢复），不是删除。",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                                if (dupRemovable > 0) {
                                    Spacer(Modifier.height(WnSpace.sm))
                                    OutlinedButton(
                                        onClick = { dupDialog = true },
                                        enabled = !dupBusy,
                                        shape = WnPillShape,
                                        border = BorderStroke(1.dp, WnColors.danger),
                                        colors = ButtonDefaults.outlinedButtonColors(
                                            contentColor = WnColors.danger),
                                        modifier = Modifier.testTag("src_dup_open"),
                                    ) {
                                        Text(if (dupBusy) "清理中…" else "查看并清理（$dupRemovable 个文件）")
                                    }
                                }
                            }
                        }
                        if (removedCount > 0) {
                            Spacer(Modifier.height(WnSpace.sm))
                            WnHintCard(borderColor = WnColors.accent,
                                modifier = Modifier.testTag("src_restore_card")) {
                                // 多批时把"一次恢复一批"讲清楚（服务端按 latest 只恢复最近一批）
                                val moreBatches =
                                    if (removedLatest > 0 && removedCount > removedLatest)
                                        "共 $removedCount 个、分多批：一次恢复一批" +
                                            "（最近一批 $removedLatest 个），想都恢复回来就多点几次。"
                                    else ""
                                Text("已清理/删除的源文件：$removedCount 个（可恢复）",
                                    style = MaterialTheme.typography.labelMedium,
                                    fontWeight = FontWeight.Medium)
                                Spacer(Modifier.height(2.dp))
                                Text("清理只移动不删除，但备份在应用私有目录里，你点不到。" +
                                    "这里可以恢复回来；已有同名文件时不会覆盖。" + moreBatches,
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                                Spacer(Modifier.height(WnSpace.sm))
                                OutlinedButton(
                                    onClick = { restoreDialog = true },
                                    enabled = !restoreBusy,
                                    shape = WnPillShape,
                                    border = BorderStroke(1.dp, WnColors.accent),
                                    colors = ButtonDefaults.outlinedButtonColors(
                                        contentColor = WnColors.accent),
                                    modifier = Modifier.testTag("src_restore_open"),
                                ) {
                                    Text(if (restoreBusy) "恢复中…" else
                                        "恢复最近一批（${if (removedLatest > 0) removedLatest else removedCount}）")
                                }
                            }
                        }
                        restoreResult?.let {
                            Spacer(Modifier.height(WnSpace.xs))
                            Text(it, style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.primary,
                                modifier = Modifier.testTag("src_restore_result"))
                        }
                        dupResult?.let {
                            Spacer(Modifier.height(WnSpace.xs))
                            Text(it, style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.primary,
                                modifier = Modifier.testTag("src_dup_result"))
                        }
                        msg?.let {
                            Spacer(Modifier.height(WnSpace.sm))
                            Text(it, style = MaterialTheme.typography.labelMedium,
                                 color = MaterialTheme.colorScheme.primary)
                        }
                        Spacer(Modifier.height(WnSpace.sm))
                        Row(verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                            OutlinedButton(
                                onClick = {
                                    scope.launch {
                                        busy = true
                                        verifyMsg = "正在启动逐源功能验证…"
                                        val start = gateway.httpPost(ep.port, "/api/sources/verify",
                                            JSONObject().put("limit", 6)
                                                .put("skip_verified_days", 7).toString())
                                        val already = runCatching {
                                            JSONObject(start.body).optBoolean("already_running")
                                        }.getOrDefault(false)
                                        if (already) {
                                            verifyMsg = "已有一轮验证在跑，跟随它的进度"
                                        } else if (start.code != 202 && start.code != 200) {
                                            verifyMsg = "启动失败：HTTP ${start.code} ${start.body.take(80)}"
                                        }
                                        // 轮询进度（服务端执行，界面只反映状态）
                                        var done = false
                                        for (i in 0 until 120) {
                                            kotlinx.coroutines.delay(2000)
                                            val st = gateway.httpText(ep.port, "/api/sources/verify/status")
                                            val o = runCatching { JSONObject(st.body) }.getOrNull() ?: continue
                                            val s2 = o.optString("status")
                                            if (s2 == "running") {
                                                verifyMsg = "验证中 ${o.optInt("done")}/${o.optInt("total")}：" +
                                                    o.optString("current")
                                                continue
                                            }
                                            if (s2 == "error") {
                                                verifyMsg = "验证异常：${o.optString("error")}"
                                                done = true
                                                break
                                            }
                                            done = true
                                            break
                                        }
                                        if (!done) verifyMsg = "验证仍在后台进行（可稍后刷新看结果）"
                                        busy = false
                                        reload()
                                        verifyMsg = (verifyMsg ?: "") +
                                            if (done) " · 结果已更新" else ""
                                    }
                                },
                                enabled = !busy,
                                shape = WnPillShape,
                                border = BorderStroke(1.dp, WnColors.accent),
                                colors = ButtonDefaults.outlinedButtonColors(
                                    contentColor = WnColors.accent),
                            ) { Text(if (busy) "验证中…" else "验证下一批（未验证源优先）") }
                            Text(
                                verifySummary?.let { it.label + " · " + it.labelAll }
                                    ?: "已通过 ${verifs.values.count { it.ok }} / 已记录 ${verifs.size}",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        verifyMsg?.let {
                            Spacer(Modifier.height(WnSpace.xs))
                            Text(it, style = MaterialTheme.typography.labelSmall,
                                 color = MaterialTheme.colorScheme.primary)
                        }
                        if (verifyMeta.first.isNotBlank()) {
                            Text(
                                "最近一轮：${verifyMeta.first}" +
                                    (if (verifyMeta.second.isNotBlank()) "（关键词 ${verifyMeta.second}）" else ""),
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        Spacer(Modifier.height(WnSpace.sm))
                    }
                    // 键必须唯一：设备上确实存在两个文件算出同一个 uid 的情况，
                    // 只用 uid 当键会让 LazyColumn 抛 IllegalArgumentException（实测踩到）
                    itemsIndexed(shown, key = { i, s -> "${s.uid}#$i" }) { _, s ->
                        BookSourceCard(
                            s = s,
                            verif = verifs[s.uid],
                            busy = busy,
                            onToggle = { want ->
                                scope.launch {
                                    msg = null
                                    val r = gateway.httpPost(ep.port,
                                        "/api/sources/${Uri.encode(s.uid)}/toggle",
                                        JSONObject().put("enabled", want).toString())
                                    msg = if (r.ok) "${s.name}：${if (want) "已启用" else "已停用"}"
                                          else "切换失败：HTTP ${r.code}"
                                    reload()
                                }
                            },
                            onValidate = {
                                scope.launch {
                                    busy = true
                                    msg = "正在校验 ${s.name}…"
                                    val r = gateway.httpPost(ep.port,
                                        "/api/sources/${Uri.encode(s.uid)}/validate")
                                    val o = runCatching { JSONObject(r.body) }.getOrNull()
                                    msg = when {
                                        !r.ok -> "校验请求失败：HTTP ${r.code}"
                                        o?.optBoolean("ok") == true -> "${s.name}：校验通过"
                                        else -> "${s.name}：校验未通过（${
                                            o?.optString("error")?.take(60) ?: "原因未知"
                                        }）"
                                    }
                                    busy = false
                                    reload()
                                }
                            },
                            onDelete = {
                                scope.launch {
                                    busy = true
                                    val r = gateway.httpDelete(
                                        ep.port, "/api/sources/${Uri.encode(s.uid)}")
                                    busy = false
                                    msg = if (r.ok) "已删除书源：${s.name}"
                                          else "删除失败：HTTP ${r.code}"
                                    reload()
                                }
                            },
                        )
                    }
                    item {
                        Spacer(Modifier.height(WnSpace.sm))
                        Text(
                            "提示：书源地址必须是公网 http/https，服务端会拒绝内网地址" +
                                "（SSRF 防护）；校验需要联网，失败原因会显示在卡片上。",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }
        }
    }

    // 恢复被清理的源：列清楚"将恢复什么、以及不覆盖同名文件"再动手
    if (restoreDialog) {
        AlertDialog(
            onDismissRequest = { if (!restoreBusy) restoreDialog = false },
            title = { Text("恢复被清理的源文件") },
            text = {
                Column(Modifier.testTag("src_restore_dialog")) {
                    Text("将把最近一次清理/删除的源文件" +
                        "（本批 ${if (removedLatest > 0) removedLatest else removedCount} 个）恢复到书源目录。",
                        style = MaterialTheme.typography.bodySmall)
                    Spacer(Modifier.height(6.dp))
                    Text("· 已有同名文件时会跳过（不覆盖你现在用的那份）",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text("· 恢复后会解除删除墓碑，内置源升级也不会再把它当'用户删过'",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text("· 更早批次里的备份不会被这次碰到，可再点一次继续恢复",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            },
            confirmButton = {
                TextButton(onClick = { restoreRemoved() }, enabled = !restoreBusy,
                    modifier = Modifier.testTag("src_restore_confirm")) {
                    Text(if (restoreBusy) "恢复中…" else "恢复")
                }
            },
            dismissButton = {
                TextButton(onClick = { restoreDialog = false },
                    modifier = Modifier.testTag("src_restore_cancel")) { Text("取消") }
            },
        )
    }

    // 重复源文件清理确认：把"保留哪一份、移走哪几份、为什么"逐条列清楚再动手
    if (dupDialog) {
        val groups = dupReport?.optJSONArray("groups")
        AlertDialog(
            onDismissRequest = { if (!dupBusy) dupDialog = false },
            title = { Text("清理重复源文件") },
            text = {
                Column(Modifier.testTag("src_dup_dialog")) {
                    Text("只移动多余的那一份到备份目录（不是删除），之后可恢复。",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(6.dp))
                    if (groups != null) {
                        for (i in 0 until groups.length()) {
                            val g = groups.optJSONObject(i) ?: continue
                            val safe = g.optBoolean("safe")
                            Text(
                                buildString {
                                    append(if (safe) "· 清理：" else "· 保留不动（需人工确认）：")
                                    append(g.optString("uid"))
                                    if (safe) {
                                        append("\n    保留 ").append(g.optString("keep"))
                                        val arr = g.optJSONArray("remove")
                                        val names = ArrayList<String>()
                                        for (j in 0 until (arr?.length() ?: 0)) {
                                            names.add(arr!!.optString(j))
                                        }
                                        append("\n    移走 ").append(names.joinToString("、"))
                                    }
                                    append("\n    ").append(g.optString("reason"))
                                },
                                style = MaterialTheme.typography.labelSmall,
                                color = if (safe) MaterialTheme.colorScheme.error
                                        else MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                            Spacer(Modifier.height(4.dp))
                        }
                    }
                }
            },
            confirmButton = {
                TextButton(
                    onClick = { cleanupDuplicates() },
                    enabled = !dupBusy,
                    modifier = Modifier.testTag("src_dup_confirm"),
                ) { Text(if (dupBusy) "清理中…" else "移到备份并清理") }
            },
            dismissButton = {
                TextButton(onClick = { dupDialog = false },
                    modifier = Modifier.testTag("src_dup_cancel")) { Text("取消") }
            },
        )
    }
}

@Composable
private fun BookSourceCard(
    s: BookSource,
    verif: SourceVerification?,
    busy: Boolean,
    onToggle: (Boolean) -> Unit,
    onValidate: () -> Unit,
    onDelete: () -> Unit,
) {
    WnHairlineCard {
        Column(Modifier.padding(WnSpace.md)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(s.name, style = MaterialTheme.typography.bodyMedium,
                         fontWeight = FontWeight.Medium, maxLines = 1,
                         overflow = TextOverflow.Ellipsis)
                    Text(
                        s.host.ifBlank { s.uid },
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 1, overflow = TextOverflow.Ellipsis,
                    )
                }
                Switch(checked = s.enabled, onCheckedChange = { onToggle(it) }, enabled = !busy)
            }
            Spacer(Modifier.height(WnSpace.sm))
            // 三轴状态徽标（pill）：未校验 / 功能验证 / 移动可用性。
            // 颜色语义：未验证 = inkDim 描边、通过 = ok、失败 = danger、部分/降级 = accent。
            Row(
                Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                // 轴一：格式校验——未校验过就是"未校验"，不冒充已通过
                when {
                    s.valid == null ->
                        WnStatusPill("未校验", WnColors.inkDim, filled = false)
                    s.valid == true ->
                        WnStatusPill("校验通过", WnColors.ok, filled = true)
                    else ->
                        WnStatusPill("校验未通过", WnColors.danger, filled = false)
                }
                // 轴二：功能验证——没有记录就是"未验证"，不靠颜色暗示"应该能用"
                when {
                    verif == null ->
                        WnStatusPill("功能验证：未验证", WnColors.inkDim, filled = false)
                    verif.ok ->
                        WnStatusPill("功能验证：" + verif.label, WnColors.ok, filled = true)
                    verif.status == "partial" ->
                        WnStatusPill("功能验证：" + verif.label, WnColors.accent, filled = false)
                    else ->
                        WnStatusPill("功能验证：" + verif.label, WnColors.danger, filled = false)
                }
                // 轴三：移动可用性台账（与漫画源页同口径）：分类 + 结论是否过期。
                // testTag 打在带完整文案的徽标上（semantics 里能读到"移动可用性：…"）
                if (s.categoryLabel.isNotBlank()) {
                    WnStatusPill(
                        text = "移动可用性：" + s.availabilityLabel +
                            (if (s.verifyStale) " · 结论已过期，建议重测" else ""),
                        color = when (s.categoryLabel) {
                            "已验证" -> WnColors.ok
                            "已停用", "待验证" -> WnColors.inkDim
                            else -> WnColors.accent
                        },
                        filled = s.categoryLabel == "已验证",
                        modifier = Modifier.testTag("src_availability_${s.uid}"),
                    )
                }
            }
            // 细节行：校验时间 / 失败原因 / 卡在哪一步 / 证据数字——全部保留，层次化收紧
            if (s.valid == true && s.validTestedAt.isNotBlank()) {
                Spacer(Modifier.height(WnSpace.xs))
                Text("校验时间：${s.validTestedAt}",
                     style = MaterialTheme.typography.labelSmall,
                     color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (s.valid == false && s.validError.isNotBlank()) {
                Spacer(Modifier.height(WnSpace.xs))
                Text("校验未通过：${s.validError.take(80)}",
                     style = MaterialTheme.typography.labelSmall,
                     color = MaterialTheme.colorScheme.error)
            }
            if (verif?.testedAt?.isNotBlank() == true) {
                Text("验证时间：${verif!!.testedAt}", style = MaterialTheme.typography.labelSmall,
                     color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (s.categoryReason.isNotBlank()) {
                Text(s.categoryReason, style = MaterialTheme.typography.labelSmall,
                     color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (s.evidenceLabel.isNotBlank()) {
                Text("实测证据：${s.evidenceLabel}",
                     style = MaterialTheme.typography.labelSmall,
                     color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(WnSpace.sm))
            Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                OutlinedButton(
                    onClick = onValidate,
                    enabled = !busy,
                    shape = WnPillShape,
                    border = BorderStroke(1.dp, WnColors.accent),
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = WnColors.accent),
                ) { Text("校验") }
                // 删除是破坏性操作：danger 色系但样式克制（描边 + 文字，不做填充）
                OutlinedButton(
                    onClick = onDelete,
                    enabled = !busy,
                    shape = WnPillShape,
                    border = BorderStroke(1.dp, WnColors.danger),
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = WnColors.danger),
                ) { Text("删除") }
            }
        }
    }
}

// ── 备份与恢复 ───────────────────────────────────────────────

@Composable
fun BackupScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    appVersion: String,
    onBack: () -> Unit,
) {
    val ctx = androidx.compose.ui.platform.LocalContext.current
    var msg by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var scopeInfo by remember { mutableStateOf<BackupScope?>(null) }
    val scope = rememberCoroutineScope()

    // 备份范围：数字来自服务端磁盘实扫（GET /api/backup/scope），
    // 不含正文/图片的部分也把**真实体积**摆出来，用户才知道备份有多"小"
    LaunchedEffect(ep) {
        val r = gateway.httpText(ep.port, "/api/backup/scope")
        if (r.ok) scopeInfo = EngineData.backupScope(r.body)
    }

    val createLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.CreateDocument("application/zip")
    ) { uri: Uri? ->
        if (uri == null) {
            msg = "已取消备份"
            return@rememberLauncherForActivityResult
        }
        scope.launch {
            busy = true; msg = "正在备份…"
            msg = try {
                val r = Backup.create(ctx, uri, appVersion)
                "备份完成：${r.files} 个文件、${Export.human(r.bytes)}（${r.createdAt}）" +
                    (r.warning?.let { "；注意：$it" } ?: "")
            } catch (t: Throwable) {
                "备份失败：${t.message ?: t.javaClass.simpleName}"
            }
            busy = false
        }
    }

    val restoreLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri: Uri? ->
        if (uri == null) {
            msg = "已取消恢复"
            return@rememberLauncherForActivityResult
        }
        scope.launch {
            busy = true; msg = "正在恢复（会先校验整份备份）…"
            msg = try {
                val r = Backup.restore(ctx, uri)
                "恢复完成：写入 ${r.restored} 个文件" +
                    (if (r.skipped > 0) "，跳过 ${r.skipped} 个（备份里缺失）" else "") +
                    "（备份时间 ${r.createdAt}）；重启引擎后生效。"
            } catch (t: Throwable) {
                "恢复失败：${t.message ?: t.javaClass.simpleName}"
            }
            busy = false
        }
    }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text("备份与恢复", maxLines = 1) },
            navigationIcon = { TextButton(onClick = onBack) { Text("← 返回") } },
        )
    }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize().padding(WnSpace.lg)) {
            WnSectionHeader("备份内容")
            Text(
                "书源配置与启用状态、小说阅读进度、漫画书库与阅读进度。\n" +
                    "不含书籍正文与漫画图片：这些体积大且可以从书源重新下载，" +
                    "因此备份文件很小，也不等于『离线可读的全库副本』。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            scopeInfo?.let { si ->
                Spacer(Modifier.height(WnSpace.sm))
                WnHintCard(borderColor = WnColors.line) {
                    Text(
                        "本机实测：将包含 ${si.srcFiles} 个书源文件" +
                            (if (si.progressFiles.isNotEmpty()) "、" +
                                si.progressFiles.size + " 个进度文件" else "") +
                            "，合计约 ${Export.human(si.includeBytes)}；" +
                            "不含的部分共 ${Export.human(si.excludedBytes)}。",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.testTag("backup_scope_summary"),
                    )
                    Spacer(Modifier.height(WnSpace.xs))
                    si.includes.forEach {
                        Text("· 包含：$it", style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    si.excludes.forEach { e ->
                        Text("· 不含：${e.first}（${Export.human(e.second)}）—— ${e.third}",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Text(si.note, style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.testTag("backup_scope_note"))
                }
            }
            Spacer(Modifier.height(WnSpace.lg))
            WnSectionHeader("操作")
            Button(onClick = { createLauncher.launch(Backup.suggestName()) },
                enabled = !busy, shape = WnPillShape,
                modifier = Modifier.fillMaxWidth()) {
                Text(if (busy) "处理中…" else "创建备份（选择保存位置）")
            }
            Spacer(Modifier.height(WnSpace.sm))
            // 恢复会覆盖同名文件（备份写入）：danger 色系但样式克制（描边 + 文字）
            OutlinedButton(onClick = {
                restoreLauncher.launch(arrayOf("application/zip", "application/octet-stream", "*/*"))
            }, enabled = !busy, shape = WnPillShape,
                border = BorderStroke(1.dp, if (busy) WnColors.line else WnColors.danger),
                colors = ButtonDefaults.outlinedButtonColors(contentColor = WnColors.danger),
                modifier = Modifier.fillMaxWidth()) {
                Text("从备份恢复（选择文件）")
            }
            Spacer(Modifier.height(WnSpace.md))
            msg?.let {
                Text(it, style = MaterialTheme.typography.bodySmall,
                     color = MaterialTheme.colorScheme.primary)
                Spacer(Modifier.height(WnSpace.sm))
            }
            Text(
                "恢复前会校验整份备份（格式版本、路径白名单、逐文件 SHA-256），" +
                    "校验不通过就整体不写入；写入中途出错会自动回滚。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
