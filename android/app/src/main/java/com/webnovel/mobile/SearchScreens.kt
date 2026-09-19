@file:OptIn(ExperimentalMaterial3Api::class)

package com.webnovel.mobile

import android.net.Uri
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowLeft
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material.icons.outlined.Search
import androidx.compose.material.icons.outlined.WifiOff
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * 漫画搜索结果按 (source, id) 去重——服务端不去重（同名不同 id 是不同作品），
 * 但"加载更多"翻页时同一部作品可能再次出现，界面不应该出现重复卡片。
 * 提成文件级纯函数：派生状态（从已保存的多页响应合并）也要用它。
 */
/**
 * 把两份"合成响应"（{"results":[...]}）合并成一份：流式搜索逐源累积时用它，
 * 复用同一套解析器，保证流式与阻塞两条路径的结果结构完全一致。
 */
/**
 * 逐源报错**限行**：报错不能把搜索结果挤出屏幕。
 *
 * 用户实测反馈（0.74.2）：小说搜索里源报错时"整个屏幕都是报错信息，无法查看
 * 搜索结果"——几十个源的失败文案是竖直堆在结果上方的。根因是我在逐源文案里
 * 追加了整句代理说明，但**限行本身是必须的**：无论文案多短，源多起来就会淹掉结果。
 *
 * 完整清单不丢：服务端 errors 元数据、以及「设置 → 导出诊断报告」里都有全文。
 */
internal fun capErrorLines(text: String, maxShown: Int = 3): String {
    if (text.isBlank()) return ""
    val lines = text.split("\n").filter { it.isNotBlank() }
    if (lines.size <= maxShown) return lines.joinToString("\n")
    return lines.take(maxShown).joinToString("\n") +
        "\n…另有 ${lines.size - maxShown} 个源失败（完整清单见「设置 → 导出诊断报告」）"
}

/** 搜索框统一配色：边框 line/accent（暖墨暗色），圆角走 WnPillShape */
@Composable
private fun wnSearchFieldColors() = OutlinedTextFieldDefaults.colors(
    focusedBorderColor = WnColors.accent,
    unfocusedBorderColor = WnColors.line,
    focusedLabelColor = WnColors.accent,
    unfocusedLabelColor = WnColors.inkDim,
    cursorColor = WnColors.accent,
)

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
 * 真断网提示条：surface 底 + 1dp danger 发丝边框 + WifiOff 图标。
 * 标题文案「本机当前没有网络」与 testTag 原样保留（testTag 由调用方传入）。
 */
@Composable
private fun OfflineBanner(body: String, titleTag: String) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = WnCardShape,
        colors = CardDefaults.cardColors(containerColor = WnColors.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
        border = BorderStroke(1.dp, WnColors.danger),
    ) {
        Row(Modifier.padding(WnSpace.md), verticalAlignment = Alignment.CenterVertically) {
            Icon(
                Icons.Outlined.WifiOff,
                contentDescription = null,
                tint = WnColors.danger,
                modifier = Modifier.size(22.dp),
            )
            Spacer(Modifier.width(WnSpace.md))
            Column {
                Text(
                    "本机当前没有网络",
                    style = MaterialTheme.typography.titleSmall,
                    fontWeight = FontWeight.SemiBold,
                    color = WnColors.ink,
                    modifier = Modifier.testTag(titleTag),
                )
                Spacer(Modifier.height(WnSpace.xs))
                Text(
                    body,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

private fun mergeBodies(a: String, b: String): String {
    val arr = org.json.JSONArray()
    for (body in listOf(a, b)) {
        if (body.isBlank()) continue
        val rs = runCatching {
            org.json.JSONObject(body).optJSONArray("results")
        }.getOrNull() ?: continue
        for (i in 0 until rs.length()) arr.put(rs.opt(i))
    }
    return org.json.JSONObject().put("results", arr).toString()
}

/**
 * 合并小说搜索的增量分组：`inc` 是**新出现**的分组，`upd` 是同 key 的**内容更新**
 * （晚到来源补齐 / 元数据变化）。key 用 name|author——与服务端分组键一致。
 * 用原始 JSON 合并（而不是解析后的对象）是为了复用同一套防御式解析。
 */
private fun mergeNovelGroups(cur: String, inc: org.json.JSONArray?,
                             upd: org.json.JSONArray?): String {
    fun keyOf(o: org.json.JSONObject) = o.optString("name") + "|" + o.optString("author")
    val order = ArrayList<String>()
    val byKey = HashMap<String, org.json.JSONObject>()
    fun putAll(arr: org.json.JSONArray?) {
        if (arr == null) return
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val k = keyOf(o)
            if (!byKey.containsKey(k)) order.add(k)
            byKey[k] = o          // 后来的（updates / 全量快照）覆盖先前的
        }
    }
    if (cur.isNotBlank()) {
        putAll(runCatching { org.json.JSONObject(cur).optJSONArray("groups") }.getOrNull())
    }
    putAll(inc)
    putAll(upd)
    val out = org.json.JSONArray()
    for (k in order) out.put(byKey[k])
    return org.json.JSONObject().put("groups", out).toString()
}

private fun mergeMangaHits(old: List<MangaSearchHit>,
                          more: List<MangaSearchHit>): List<MangaSearchHit> {
    val seen = old.map { it.source + ":" + it.comicId }.toMutableSet()
    val out = old.toMutableList()
    for (h in more) {
        val k = h.source + ":" + h.comicId
        if (seen.add(k)) out.add(h)
    }
    return out
}

/**
 * 原生小说搜索（替代"打开网页搜索页"）：
 *   输入关键词 → 跨源搜索（服务端按书分组） → 结果里每个源都能一键"加入书架"（创建下载任务）。
 *
 * 诚实处理：搜索可能"部分源失败/超时"，服务端会给出 partial 与超时源数，
 * 界面必须显示出来——否则用户会以为"全网只有这几本"。
 * 加入书架**不在这里下载内容**：只创建任务，进度在下载页看；已存在运行中任务时
 * 服务端返回 409，界面照实提示，不假装成功。
 */
@Composable
internal fun NovelSearchScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    onOpen: (Dest) -> Unit,
    onBack: () -> Unit,
) {
    // P0-E：查询词与最近一次结果（原始响应）跨页面进出恢复，返回时结果还在
    var keyword by rememberSaveable { mutableStateOf("") }
    var body by rememberSaveable { mutableStateOf("") }
    var searching by remember { mutableStateOf(false) }
    var searched by rememberSaveable { mutableStateOf(false) }
    var msg by rememberSaveable { mutableStateOf("") }

    // 流式：逐源到达就上屏（小说端同样有 /api/search/stream；此前 App 用的是
    // 阻塞端点，得等最慢源）。groups 按 key 合并，finished 时用全量快照校准。
    var streamGroups by rememberSaveable { mutableStateOf("") }
    var streamErrors by rememberSaveable { mutableStateOf("") }
    var streamDone by rememberSaveable { mutableStateOf(-1) }
    var streamTotal by rememberSaveable { mutableIntStateOf(0) }
    var streamCached by rememberSaveable { mutableStateOf(false) }
    // 真断网：服务端判定"整轮无结果且本机连不出去"（engine/neterr.py）
    var streamOffline by rememberSaveable { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    // 结果来源优先流式累积（body 只作恢复用）：两者结构一致，走同一套解析
    val parsed: Pair<List<NovelSearchGroup>, SearchMeta> =
        remember(streamGroups, body) {
            val src = if (streamGroups.isNotBlank()) streamGroups else body
            if (src.isBlank()) emptyList<NovelSearchGroup>() to SearchMeta()
            else EngineData.novelSearch(src)
        }
    val groups: List<NovelSearchGroup> = parsed.first
    val meta: SearchMeta = parsed.second

    fun doSearch() {
        val q = keyword.trim()
        if (q.isEmpty()) {
            msg = "请输入关键词"
            return
        }
        scope.launch {
            searching = true; msg = ""
            streamGroups = ""; streamErrors = ""
            streamDone = 0; streamTotal = 0; streamCached = false
            streamOffline = false
            val path = "/api/search/stream?q=${Uri.encode(q)}"
            val code = gateway.streamEvents(ep.port, path) { evt ->
                val fin = evt.optBoolean("finished")
                if (fin) {
                    // 最终事件：groups 是**全量快照**（用它校准增量期间的合并结果），
                    // errors 是"源名 → 失败/超时原因"，据此区分"源失败"与"真没结果"
                    val snap = evt.optJSONArray("groups")
                    if (snap != null) {
                        streamGroups = JSONObject().put("groups", snap).toString()
                    }
                    val errs = evt.optJSONObject("errors")
                    streamErrors = buildString {
                        if (errs != null) {
                            for (k in errs.keys()) {
                                if (isNotEmpty()) append("\n")
                                append("· ").append(k).append("：")
                                    .append(errs.optString(k))
                            }
                        }
                    }
                    streamOffline = evt.optBoolean("network_down")
                    streamDone = evt.optInt("done"); streamTotal = evt.optInt("total")
                    if (evt.optBoolean("cached")) streamCached = true
                } else {
                    // 增量：新出现分组 + 已有分组的内容更新（同书晚到来源补齐）
                    val inc = evt.optJSONArray("groups")
                    val upd = evt.optJSONArray("updates")
                    if ((inc?.length() ?: 0) > 0 || (upd?.length() ?: 0) > 0) {
                        streamGroups = mergeNovelGroups(streamGroups, inc, upd)
                    }
                    streamDone = evt.optInt("done"); streamTotal = evt.optInt("total")
                }
            }
            if (code !in 200..299) {
                // 流式不可用 → 明确回落阻塞端点（不静默失败）
                msg = "流式搜索不可用（HTTP $code），已改用一次性搜索"
                val r = gateway.httpText(ep.port, "/api/search?q=${Uri.encode(q)}")
                if (!r.ok) {
                    msg = "搜索失败：HTTP ${r.code}" +
                        if (r.body.isNotBlank()) " · ${r.body.take(120)}" else ""
                    body = ""
                } else {
                    body = r.body
                }
            }
            searched = true
            searching = false
        }
    }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text("小说搜索", maxLines = 1) },
            navigationIcon = { TextButton(onClick = onBack) { Text("← 返回") } },
        )
    }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize().padding(WnSpace.md)) {
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                OutlinedTextField(
                    value = keyword,
                    onValueChange = { keyword = it },
                    label = { Text("书名或作者") },
                    singleLine = true,
                    shape = WnPillShape,
                    colors = wnSearchFieldColors(),
                    modifier = Modifier.weight(1f).testTag("novel_search_field"),
                )
                Button(onClick = { doSearch() }, enabled = !searching,
                    shape = WnPillShape,
                    modifier = Modifier.testTag("novel_search_btn")) {
                    Text(if (searching) "搜索中…" else "搜索")
                }
            }
            // 流式进度：让用户看到结果正陆续到达，而不是盯着转圈（小说端此前要等最慢源）
            if (searching && streamTotal > 0) {
                Spacer(Modifier.height(6.dp))
                Text("已返回 $streamDone/$streamTotal 个源 · 已显示 ${groups.size} 本",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.testTag("novel_search_progress"))
            }
            if (searched) {
                Spacer(Modifier.height(6.dp))
                Text(
                    buildString {
                        append("共 ${groups.size} 本书")
                        if (streamTotal > 0) append("（$streamDone/$streamTotal 个源已返回）")
                        if (streamCached) append(" · 缓存")
                        if (meta.timedOutSources > 0) append(" · ${meta.timedOutSources} 个源超时未回")
                        if (meta.partial) append("（部分源仍在返回，可重试或稍后再搜）")
                    },
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (streamErrors.isNotBlank()) {
                    // 逐源失败/超时原因：把"源失败"与"真没搜到"分开（不静默当没有结果）
                    Text(capErrorLines(streamErrors),
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.error,
                        modifier = Modifier.testTag("novel_search_errors"))
                }
            }
            if (msg.isNotBlank()) {
                Spacer(Modifier.height(6.dp))
                Text(msg, color = MaterialTheme.colorScheme.error,
                     style = MaterialTheme.typography.bodySmall)
            }
            Spacer(Modifier.height(8.dp))
            when {
                searching && groups.isEmpty() ->
                    Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
                searched && groups.isEmpty() ->
                    Column(Modifier.fillMaxWidth().padding(top = WnSpace.lg)) {
                        if (meta.networkDown || streamOffline) {
                            // 真断网时"没有结果"是错的归因：用户会以为书源都坏了
                            OfflineBanner(
                                body = "搜索要连书源，需要网络。已下载的小说不受影响，" +
                                    "仍可在书架里离线阅读；恢复网络后点「重试」即可。",
                                titleTag = "novel_search_offline",
                            )
                            Spacer(Modifier.height(WnSpace.md))
                            Button(onClick = { doSearch() }, shape = WnPillShape) {
                                Text("重试")
                            }
                        } else {
                            WnEmptyState(
                                icon = Icons.Outlined.Search,
                                title = "没有结果",
                                body = "可能是关键词太偏，或启用书源当时都失败了。" +
                                    "可在「设置 → 书源管理」看看哪些源已启用/通过功能验证。",
                            ) {
                                Button(onClick = { doSearch() }, shape = WnPillShape) {
                                    Text("重试")
                                }
                            }
                        }
                    }
                else -> LazyColumn(Modifier.fillMaxSize().testTag("novel_search_results"),
                    verticalArrangement = Arrangement.spacedBy(10.dp),
                    contentPadding = PaddingValues(bottom = 16.dp)) {
                    items(groups, key = { it.name + "|" + it.author }) { g ->
                        NovelGroupCard(g, enabled = !searching,
                            onAdd = { src ->
                                scope.launch {
                                    // 只创建任务，不在这里下载；服务端按 (源, 书地址) 去重
                                    val body = JSONObject()
                                        .put("source_uid", src.sourceUid)
                                        .put("book_url", src.bookUrl).toString()
                                    val r = gateway.httpPost(ep.port, "/api/tasks", body)
                                    msg = if (r.code == 202) {
                                        "已加入书架并开始下载：${g.name}（${src.sourceName}）" +
                                            "，进度见「下载」页"
                                    } else {
                                        "加入书架失败：HTTP ${r.code} " + r.body.take(120)
                                    }
                                }
                            })
                    }
                }
            }
        }
    }
}

@Composable
private fun NovelGroupCard(g: NovelSearchGroup, enabled: Boolean, onAdd: (NovelSearchSource) -> Unit) {
    WnHairlineCard {
        Column(Modifier.padding(WnSpace.md)) {
            Text(g.name.ifBlank { "（无书名）" }, style = MaterialTheme.typography.bodyLarge,
                 fontWeight = FontWeight.Medium, maxLines = 2, overflow = TextOverflow.Ellipsis)
            Spacer(Modifier.height(WnSpace.xs))
            Text(
                g.author.ifBlank { "作者未知" } + " · ${g.sources.size} 个来源",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (g.intro.isNotBlank()) {
                Spacer(Modifier.height(WnSpace.xs))
                Text(g.intro, style = MaterialTheme.typography.bodySmall, maxLines = 3,
                     overflow = TextOverflow.Ellipsis)
            }
            Spacer(Modifier.height(WnSpace.sm))
            HorizontalDivider(color = WnColors.line)
            g.sources.forEach { src ->
                Row(Modifier.fillMaxWidth().padding(vertical = WnSpace.sm),
                    verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(src.sourceName.ifBlank { src.sourceUid },
                             style = MaterialTheme.typography.bodySmall,
                             fontWeight = FontWeight.Medium)
                        Text(
                            buildString {
                                if (src.chapterCount > 0) append("${src.chapterCount} 章")
                                if (src.lastChapter.isNotBlank()) {
                                    if (isNotEmpty()) append(" · ")
                                    append("最新：${src.lastChapter}")
                                }
                                if (src.wordCount.isNotBlank()) append(" · ${src.wordCount}")
                            }.ifBlank { "（无章节信息）" },
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            maxLines = 1, overflow = TextOverflow.Ellipsis,
                        )
                    }
                    OutlinedButton(
                        onClick = { onAdd(src) },
                        enabled = enabled,
                        shape = WnPillShape,
                        border = BorderStroke(1.dp,
                            if (enabled) WnColors.accent else WnColors.line),
                        colors = ButtonDefaults.outlinedButtonColors(
                            contentColor = WnColors.accent),
                    ) { Text("加入书架") }
                }
            }
        }
    }
}

/**
 * 原生漫画搜索：输入关键词 → 跨源搜索 → 点结果直接进**原生详情页**（再下载/阅读）。
 * 失败源单独列出（errors），与"确实没有结果"区分开。
 */
@Composable
internal fun MangaSearchScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    loader: coil.ImageLoader,
    onOpen: (Dest) -> Unit,
    onBack: () -> Unit,
    // 从单源页进来时带上源 key（"在此源搜索"）；为空 = 全部源
    presetSource: String = "",
    presetOrder: String = "",
) {
    // P0-E：查询词与"已加载页的原始响应"必须能跨页面进出恢复。
    // 保存原始 JSON 而不是解析后的对象：省掉自定义 Saver，且恢复时复用同一套
    // 防御式解析（不会出现"保存的结构与解析器不一致"这种隐蔽错误）。
    var keyword by rememberSaveable { mutableStateOf("") }
    // 0.67.0：改成**网页端同款的分页**——一次只显示"当前页"，翻页是替换而不是追加。
    // 旧实现把每页原始响应追加进 pages 再合并显示（"加载更多"），既没有页码，
    // 也因为没有更新页码状态而永远只能再翻一页（用户 2026-09-17 反馈）。
    var pageBody by rememberSaveable { mutableStateOf("") }   // 当前页（page>1）的原始响应
    var page by rememberSaveable { mutableIntStateOf(1) }     // 当前页码
    var searching by remember { mutableStateOf(false) }
    var searched by rememberSaveable { mutableStateOf(false) }
    var loadingMore by remember { mutableStateOf(false) }
    var msg by rememberSaveable { mutableStateOf("") }
    // P0-2：分源搜索。源与排序都必须跨页面恢复（返回后不能丢），并且**每条请求路径**
    // 都要带同一套参数（首搜/流式/翻页/回退）——服务端早就有 source 能力，
    // 以前 App 根本没接（0.51.0 风险评估 §3）。
    var sourceKey by rememberSaveable { mutableStateOf(presetSource) }
    var orderKey by rememberSaveable { mutableStateOf(presetOrder) }
    // 源清单（含依赖判定/实测结论/是否混淆源/是否支持排序）——没加载出来时只给"全部源"
    var sourceCards by remember { mutableStateOf<List<MangaSource>>(emptyList()) }
    var sourcesLoaded by remember { mutableStateOf(false) }

    // 流式汇总事实（has_more/total_hits 等）与逐源错误
    var pageMeta by rememberSaveable { mutableStateOf("") }
    var errorsText by rememberSaveable { mutableStateOf("") }
    var streamCached by rememberSaveable { mutableStateOf(false) }
    // 流式进度：已返回的源数 / 总源数（null = 未在流式搜索中）
    var streamDone by rememberSaveable { mutableStateOf(-1) }
    var streamTotal by rememberSaveable { mutableIntStateOf(0) }
    // 流式过程中逐源累积的结果（以"合成响应"形式保存，复用同一套解析）
    var streamBody by rememberSaveable { mutableStateOf("") }
    // 失败时的"自检"结果：写在我们能看见失败的地方，用户不必猜到设置里去找
    var selfCheckBusy by remember { mutableStateOf(false) }
    var selfCheckMsg by rememberSaveable { mutableStateOf("") }

    val scope = rememberCoroutineScope()

    // 已加载页 + **流式实时累积** → 去重后的结果列表。
    // 关键：流式过程中就把它算进来，结果才能"随源到达陆续上屏"；
    // 等流结束再显示等于没做成流式（实测：首屏要等 20s，与修复前一样慢）。
    // 当前页的原始响应：第 1 页优先用流式累积体（实时上屏），其余页用该页自己的响应。
    val currentBody: String = if (page <= 1) {
        streamBody.ifBlank { pageBody }
    } else {
        pageBody.ifBlank { streamBody }
    }
    val hits: List<MangaSearchHit> = remember(currentBody) {
        if (currentBody.isBlank()) emptyList()
        else EngineData.mangaSearch(currentBody).first
    }
    val meta: SearchMeta = remember(currentBody, pageMeta) {
        val body = if (page <= 1) pageMeta.ifBlank { currentBody } else currentBody
        if (body.isBlank()) SearchMeta() else EngineData.mangaSearch(body).second
    }

    /**
     * 搜第 1 页时用**流式端点**：每个源完成就把它那部分结果先显示出来，
     * 而不是等所有源跑完（实测桌面端阻塞端点要等 6s、不可达源曾拖到 20s；
     * 网页端早就用流式端点绕开，原生 App 这版对齐）。
     * 翻页（page>1）仍走阻塞端点：那一页只在用户点「加载更多」时需要完整结果。
     */
    /** 当前选择的源卡片（null = 全部源或清单未加载） */
    val selected: MangaSource? = remember(sourceKey, sourceCards) {
        sourceCards.firstOrNull { it.key == sourceKey }
    }

    /**
     * 搜索参数（唯一来源）：首搜、流式、翻页、流式回退**全部**用它拼路径。
     * 任何一条路径漏带 source 都会串源（以前整页都没带，是用户报"分源搜索失效"的主因）。
     */
    fun searchQuery(suffix: String): String = buildString {
        append(suffix)
        if (sourceKey.isNotBlank()) append("&source=").append(Uri.encode(sourceKey))
        // 与网页端对齐：网页恒发 order=mr（它的"默认"就是 mr）。App 缺省也发 mr ——
        // 统一服务端搜索缓存命名空间，App 才能命中网页已预热好的搜索缓存（秒开）；
        // 非 jm 源服务端本就忽略 order，行为不变。
        append("&order=").append(Uri.encode(orderKey.ifBlank { "mr" }))
    }

    // 换源/换排序必须**清掉上一轮结果**：否则会出现"选了 A 源，屏幕上还挂着 B 源的结果"，
    // 用户无法判断到底搜的是谁（这正是"分源搜索失效"的观感来源之一）。
    // 注意：必须区分"用户换了源"与"页面重新进入"——进入时 rememberSaveable 会恢复
    // sourceKey/searched/结果，如果只按 key 变化触发，重新进入这一屏就会把**刚恢复的
    // 结果**当成"换源"清掉（实测被 SearchUiTest 的"返回后结果必须保留"抓到）。
    var lastSource by remember { mutableStateOf(sourceKey) }
    var lastOrder by remember { mutableStateOf(orderKey) }
    LaunchedEffect(sourceKey, orderKey) {
        val changed = sourceKey != lastSource || orderKey != lastOrder
        lastSource = sourceKey
        lastOrder = orderKey
        if (changed && (searched || pageBody.isNotBlank() || streamBody.isNotBlank())) {
            pageBody = ""
            page = 1
            streamBody = ""
            pageMeta = ""
            errorsText = ""
            searched = false
            msg = ""
        }
    }

    LaunchedEffect(ep, sourcesLoaded) {
        if (!sourcesLoaded) {
            val r = gateway.httpText(ep.port, "/api/manga/sources")
            if (r.ok) {
                sourceCards = EngineData.sources(r.body)
                sourcesLoaded = true
            }
        }
    }

    fun doSearch(nextPage: Int = 1) {
        val q = keyword.trim()
        if (q.isEmpty()) {
            msg = "请输入关键词"
            return
        }
        // 禁漫码识别：6/7 位纯数字 = JM 漫画 id，直接打开对应详情页（不走搜索）
        if (nextPage <= 1 && q.matches(Regex("\\d{6,7}")) &&
            (sourceKey.isBlank() || sourceKey == "jm")) {
            onOpen(Dest.MangaDetail("jm", q))
            return
        }
        if (selected?.status == "unsupported") {
            // 本机能力缺口：不发请求、如实说明缺什么（不把失败归成"源失效"）
            msg = "该源在当前安装包内不可用：${selected.reason.ifBlank { "缺少运行依赖" }}"
            return
        }
        scope.launch {
            if (nextPage <= 1) searching = true else loadingMore = true
            msg = ""
            if (nextPage > 1) {
                val r = gateway.httpText(ep.port,
                    searchQuery("/api/manga/search?q=${Uri.encode(q)}&page=$nextPage"))
                if (!r.ok) {
                    msg = "搜索失败：HTTP ${r.code}" +
                        if (r.body.isNotBlank()) " · ${r.body.take(120)}" else ""
                } else {
                    // 网页端同款：翻页是**替换当前页**，不追加、不合并。
                    // 页内事实（页码/是否还有更多）直接取自这一页自己的响应。
                    pageBody = r.body
                    page = nextPage
                }
                searched = true; searching = false; loadingMore = false
                return@launch
            }
            // ── 第 1 页：流式 ──
            page = 1; pageBody = ""
            streamBody = ""; streamDone = 0; streamTotal = 0
            val path = searchQuery("/api/manga/search/stream?q=${Uri.encode(q)}&page=1")
            val code = gateway.streamEvents(ep.port, path) { ev ->
                if (ev.optBoolean("finished")) {
                    // **缓存命中**时服务端把全量结果放在 finished 事件的 groups 里
                    // （只发一条事件）。不并入就会把"缓存里有结果"显示成"没有结果"——
                    // 实测被"缓存命中必须仍有结果"这条断言抓到。
                    val finGroups = ev.optJSONArray("groups")
                    if (finGroups != null && finGroups.length() > 0) {
                        streamBody = mergeBodies(streamBody,
                            org.json.JSONObject().put("results", finGroups).toString())
                    }
                    // 最终事件：带上 errors / has_more / total 等汇总事实
                    val errs = ev.optJSONObject("errors")
                    val errText = buildString {
                        if (errs != null) {
                            for (k in errs.keys()) {
                                if (isNotEmpty()) append("\n")
                                append("· ").append(k).append("：")
                                    .append(errs.optString(k))
                            }
                        }
                    }
                    val summary = org.json.JSONObject()
                        .put("results", org.json.JSONArray())
                        .put("has_more", ev.optBoolean("has_more"))
                        .put("total_hits", ev.optInt("total_hits"))
                        .put("page", ev.optInt("page", 1))
                        .put("page_size", ev.optInt("page_size", 30))
                        // 真断网：服务端只在有证据时才置位（engine/neterr.py）。
                        // 带过来才能在界面上说"本机没网"，而不是"没有结果"
                        .put("network_down", ev.optBoolean("network_down"))
                    pageMeta = summary.toString()
                    errorsText = errText
                    streamDone = ev.optInt("done"); streamTotal = ev.optInt("total")
                    if (ev.optBoolean("cached")) streamCached = true
                } else {
                    // 进度事件：这个源的结果先显示出来
                    val groups = ev.optJSONArray("groups")
                    if (groups != null && groups.length() > 0) {
                        val merged = mergeBodies(streamBody,
                            org.json.JSONObject().put("results", groups).toString())
                        streamBody = merged
                    }
                    ev.optString("err").takeIf { it.isNotBlank() }?.let {
                        errorsText = (errorsText + (if (errorsText.isBlank()) "" else "\n") +
                            "· " + ev.optString("source_name", ev.optString("source")) + "：" + it)
                    }
                    streamDone = ev.optInt("done"); streamTotal = ev.optInt("total")
                }
            }
            if (code in 200..299) {
                // 流式累积体就是"第 1 页"的结果，保持它在 streamBody 里（实时上屏），
                // 翻页时会被对应页的响应替换显示。
                page = 1
            } else {
                // 流式不可用（代理缓冲/中间层不支持）→ 明确回落阻塞端点，不静默失败
                msg = "流式搜索不可用（HTTP $code），已改用一次性搜索"
                val r = gateway.httpText(ep.port,
                    searchQuery("/api/manga/search?q=${Uri.encode(q)}&page=1"))
                if (!r.ok) {
                    msg = "搜索失败：HTTP ${r.code}" +
                        if (r.body.isNotBlank()) " · ${r.body.take(120)}" else ""
                } else {
                    pageBody = r.body      // 回落后第 1 页用阻塞响应
                    page = 1
                }
            }
            searched = true; searching = false; loadingMore = false
        }
    }

    /**
     * 失败现场的**一键自检**（方向基线 §7.1：开发侧负责把故障钉到具体一层，
     * 不能让用户自己判断是哪一层）。
     *
     * 跑的是引擎里已有的逐源**功能级**验证（搜索 → 详情/目录 → 章节图片 URL →
     * 真实取一张图）：结果既显示给用户看，也会写进可导出的诊断报告——
     * 用户点一次就能把"卡在哪一层"带回来。
     */
    fun runSelfCheck() {
        scope.launch {
            selfCheckBusy = true
            selfCheckMsg = "正在启动自检…"
            // skip_verified_days=0：自检要的是**现在**的真实情况，
            // 不能因为"一周前验过"就跳过（那正是用户来点自检的原因）
            val start = gateway.httpPost(ep.port, "/api/manga/sources/verify",
                JSONObject().put("limit", 4)
                    .put("skip_verified_days", 0)
                    .put("skip_tested_days", 0).toString())
            if (start.code != 202 && start.code != 200) {
                selfCheckMsg = "自检启动失败：HTTP ${start.code} ${start.body.take(80)}"
                selfCheckBusy = false
                return@launch
            }
            var done = false
            for (i in 0 until 150) {
                kotlinx.coroutines.delay(1500)
                val st = gateway.httpText(ep.port, "/api/manga/sources/verify/status")
                val o = runCatching { JSONObject(st.body) }.getOrNull() ?: continue
                selfCheckMsg = "自检中 " + o.optInt("done") + "/" + o.optInt("total") + "：" +
                    o.optString("current").take(40)
                if (o.optString("status") == "done") { done = true; break }
                if (o.optString("status") == "error") {
                    selfCheckMsg = "自检异常：" + o.optString("error").take(80)
                    selfCheckBusy = false
                    return@launch
                }
            }
            val rr = gateway.httpText(ep.port, "/api/manga/sources/verify/results")
            val items = runCatching { JSONObject(rr.body).optJSONArray("items") }.getOrNull()
            val sb = StringBuilder()
            if (items == null || items.length() == 0) {
                sb.append("自检没有产出结果（可能是引擎重启过）；可稍后再试")
            } else {
                for (i in 0 until items.length()) {
                    val o = items.optJSONObject(i) ?: continue
                    val nm = o.optString("name").ifBlank { o.optString("key") }
                    val status = o.optString("status")
                    sb.append("· ").append(nm).append("：").append(
                        when (status) {
                            "verified" -> "可用（四步都拿到真实数据）"
                            "partial" -> "部分可用"
                            "unsupported" -> "缺运行依赖"
                            "failed" -> "失败"
                            else -> status
                        })
                    val stages = o.optJSONObject("stages")
                    val firstFail = listOf("search", "detail", "images").firstOrNull { k ->
                        val stg = stages?.optJSONObject(k)
                        stg != null && !stg.optBoolean("ok")
                    }
                    if (firstFail != null) {
                        sb.append("（").append(firstFail).append(" 阶段：")
                            .append(stages!!.optJSONObject(firstFail)!!
                                .optString("detail").take(70)).append("）")
                    }
                    sb.append('\n')
                }
            }
            if (!done) sb.append("（自检仍在后台进行，可稍后重跑）")
            sb.append("把「设置 → 高级 · 诊断 → 导出诊断报告」发给我们，就能定位卡在哪一层。")
            selfCheckMsg = sb.toString()
            selfCheckBusy = false
        }
    }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text("漫画搜索", maxLines = 1) },
            navigationIcon = { TextButton(onClick = onBack) { Text("← 返回") } },
        )
    }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize().padding(WnSpace.md)) {
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                OutlinedTextField(
                    value = keyword,
                    onValueChange = { keyword = it },
                    label = { Text("漫画名") },
                    singleLine = true,
                    shape = WnPillShape,
                    colors = wnSearchFieldColors(),
                    modifier = Modifier.weight(1f).testTag("manga_search_field"),
                )
                Button(onClick = { doSearch() }, enabled = !searching,
                    shape = WnPillShape,
                    modifier = Modifier.testTag("manga_search_btn")) {
                    Text(if (searching) "搜索中…" else "搜索")
                }
            }
            // ── P0-2 分源搜索：源选择（"全部源" + 各内置源，带依赖判定/实测结论）──
            Spacer(Modifier.height(6.dp))
            Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically) {
                WnFilterChip(
                    selected = sourceKey.isBlank(),
                    onClick = { sourceKey = ""; orderKey = "" },
                    label = "全部源",
                    modifier = Modifier.testTag("manga_src_all"),
                )
                for (card in sourceCards) {
                    val suffix = when (card.status) {
                        "unsupported" -> "缺依赖"
                        "degraded" -> "降级"
                        "supported" -> ""
                        "pending" -> "待验证"
                        else -> card.status
                    }
                    WnFilterChip(
                        selected = sourceKey == card.key,
                        onClick = {
                            sourceKey = card.key
                            // 换源即清排序：不同源支持的排序不同，沿用会让人以为"排序没生效"
                            if (!card.supportsOrder) orderKey = ""
                        },
                        label = if (suffix.isBlank()) card.name else "${card.name} · $suffix",
                        modifier = Modifier.testTag("manga_src_${card.key}"),
                    )
                }
            }
            if (selected != null) {
                Spacer(Modifier.height(2.dp))
                Text(
                    buildString {
                        append("当前源：").append(selected.name)
                        append("（依赖判定：").append(selected.status)
                        if (selected.reason.isNotBlank()) append(" · ").append(selected.reason)
                        append("；实测：").append(selected.verifyLabel).append("）")
                    },
                    style = MaterialTheme.typography.labelSmall,
                    color = if (selected.status == "unsupported") MaterialTheme.colorScheme.error
                            else MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.testTag("manga_src_note"),
                )
                if (selected.supportsOrder) {
                    Spacer(Modifier.height(4.dp))
                    Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                        horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        val orders = listOf("" to "默认", "mr" to "最新", "mv" to "最多浏览",
                            "mp" to "最多图片", "tf" to "今日最多", "tr" to "本周最多",
                            "md" to "本月最多", "pa" to "最多喜欢")
                        for ((k, label) in orders) {
                            WnFilterChip(
                                selected = orderKey == k,
                                onClick = { orderKey = k },
                                label = label,
                                modifier = Modifier.testTag("manga_order_${k.ifBlank { "default" }}"),
                            )
                        }
                    }
                }
            }
            // 流式进度：让用户看到"结果正在陆续到达"，而不是盯着一个转圈
            if (searching && streamTotal > 0) {
                Spacer(Modifier.height(6.dp))
                Text("已返回 $streamDone/$streamTotal 个源 · 已显示 ${hits.size} 条",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.testTag("manga_search_progress"))
            }
            if (searched) {
                Spacer(Modifier.height(6.dp))
                Text(
                    buildString {
                        // 与网页端同款的状态行：「第 N 页 · X 部」（网页端就是这一句）
                        append("第 $page 页 · ${hits.size} 部")
                        if (sourceKey.isNotBlank()) append(" · 源：${selected?.name ?: sourceKey}")
                        if (streamTotal > 0) append("（$streamDone/$streamTotal 个源已返回）")
                        if (streamCached) append(" · 缓存")
                        if (meta.errors.isNotEmpty()) append(" · ${meta.errors.size} 个源失败")
                    },
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                meta.errors.entries.take(2).forEach { (k, v) ->
                    Text("· $k：${v.take(60)}", style = MaterialTheme.typography.labelSmall,
                         color = MaterialTheme.colorScheme.error)
                }
                if (errorsText.isNotBlank()) {
                    // 流式路径的逐源说明（"仍在查询/超时/失败"）——不静默当没有结果
                    Text(capErrorLines(errorsText),
                         style = MaterialTheme.typography.labelSmall,
                         color = MaterialTheme.colorScheme.error,
                         modifier = Modifier.testTag("manga_search_errors"))
                }
                if (hits.isNotEmpty()) {
                    // **网页端同款分页**（0.67.0）：◀ 上一页 + 页码 + 下一页 ▶。
                    // 页码窗口只往后延伸一页（源站不给 total，更远的页数无从得知），
                    // 用「…」表示"还有/更早还有" —— 与网页端 `updatePager()` 同一套规则。
                    // 放在列表上方：懒加载列表里的离屏项在测试里不可点。
                    Spacer(Modifier.height(6.dp))
                    val busy = loadingMore || searching
                    val start = maxOf(1, page - 2)
                    val end = if (meta.hasMore) page + 1 else page
                    Row(verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(6.dp),
                        modifier = Modifier.horizontalScroll(rememberScrollState())
                            .testTag("manga_search_pager")) {
                        IconButton(
                            onClick = { doSearch(nextPage = page - 1) },
                            enabled = !busy && page > 1,
                            modifier = Modifier.defaultMinSize(48.dp, 48.dp)
                                .testTag("manga_search_prev"),
                        ) {
                            Icon(
                                Icons.AutoMirrored.Filled.KeyboardArrowLeft,
                                contentDescription = "上一页",
                                tint = if (!busy && page > 1) WnColors.accent
                                       else WnColors.inkDim,
                            )
                        }
                        if (start > 1) {
                            PagerNum(1, page, busy) { doSearch(nextPage = it) }
                            Text("…", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        for (p in start..end) {
                            PagerNum(p, page, busy) { doSearch(nextPage = it) }
                        }
                        if (meta.hasMore) {
                            Text("…", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        IconButton(
                            onClick = { doSearch(nextPage = page + 1) },
                            enabled = !busy && meta.hasMore,
                            modifier = Modifier.defaultMinSize(48.dp, 48.dp)
                                .testTag("manga_search_next"),
                        ) {
                            if (loadingMore) {
                                CircularProgressIndicator(
                                    modifier = Modifier.size(18.dp),
                                    strokeWidth = 2.dp,
                                    color = WnColors.accent,
                                )
                            } else {
                                Icon(
                                    Icons.AutoMirrored.Filled.KeyboardArrowRight,
                                    contentDescription = "下一页",
                                    tint = if (!busy && meta.hasMore) WnColors.accent
                                           else WnColors.inkDim,
                                )
                            }
                        }
                        if (loadingMore) {
                            Text("加载中…",
                                 style = MaterialTheme.typography.labelSmall,
                                 color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        if (!meta.hasMore) {
                            Text("已到最后一页",
                                 style = MaterialTheme.typography.labelSmall,
                                 color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                }
            }
            // 搜索进行中也把逐源失败原因显示出来：真断网时源 1s 内就报错，
            // 让用户马上看到"本机网络"原因，而不是盯着进度等收尾（收尾还要等
            // 服务端提前结束或源站期限）
            if (!searched && errorsText.isNotBlank()) {
                Spacer(Modifier.height(4.dp))
                Text(capErrorLines(errorsText),
                     style = MaterialTheme.typography.labelSmall,
                     color = MaterialTheme.colorScheme.error,
                     modifier = Modifier.testTag("manga_search_errors_live"))
            }
            if (msg.isNotBlank()) {
                Spacer(Modifier.height(6.dp))
                Text(msg, color = MaterialTheme.colorScheme.error,
                     style = MaterialTheme.typography.bodySmall)
            }
            Spacer(Modifier.height(8.dp))
            when {
                searching && hits.isEmpty() ->
                    Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
                searched && hits.isEmpty() ->
                    Column(Modifier.fillMaxWidth().padding(top = WnSpace.lg)) {
                        if (meta.networkDown) {
                            // 真断网（飞行模式/无信号）：说"没有结果"是错的归因——
                            // 用户会以为源站全坏了。本机已下载的内容不受影响。
                            OfflineBanner(
                                body = "搜索要连源站，需要网络。已下载的漫画/小说不受影响，" +
                                    "仍可在书架里离线阅读；恢复网络后点「重试」即可。",
                                titleTag = "manga_search_offline",
                            )
                            Spacer(Modifier.height(WnSpace.md))
                            Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                                Button(onClick = { doSearch() }, shape = WnPillShape) {
                                    Text("重试")
                                }
                                OutlinedButton(onClick = { runSelfCheck() },
                                    enabled = !selfCheckBusy,
                                    shape = WnPillShape,
                                    modifier = Modifier.testTag("manga_search_selfcheck")) {
                                    Text(if (selfCheckBusy) "自检中…" else "运行漫画源自检")
                                }
                            }
                            if (selfCheckMsg.isNotBlank()) {
                                Spacer(Modifier.height(WnSpace.sm))
                                Text(selfCheckMsg,
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    modifier = Modifier.testTag("manga_search_selfcheck_result"))
                            }
                        } else {
                            WnEmptyState(
                                icon = Icons.Outlined.Search,
                                title = "没有结果",
                                body = if (meta.errors.isNotEmpty() || errorsText.isNotBlank()) {
                                    "所有源都失败了（原因见上），不是「没有这部漫画」。"
                                } else {
                                    "换个关键词试试，或在「设置 → 书源管理」确认漫画源可用。"
                                },
                            ) {
                                Button(onClick = { doSearch() }, shape = WnPillShape) {
                                    Text("重试")
                                }
                                OutlinedButton(onClick = { runSelfCheck() },
                                    enabled = !selfCheckBusy,
                                    shape = WnPillShape,
                                    modifier = Modifier.testTag("manga_search_selfcheck")) {
                                    Text(if (selfCheckBusy) "自检中…" else "运行漫画源自检")
                                }
                                if (selfCheckMsg.isNotBlank()) {
                                    Text(selfCheckMsg,
                                        style = MaterialTheme.typography.labelSmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                        modifier = Modifier.testTag("manga_search_selfcheck_result"))
                                }
                            }
                        }
                    }
                hits.isNotEmpty() -> {
                    // （预取已移除：结果一到就并发预取封面/详情，会把封面代理 3 路闸
                    //  打满出 503、并让 jm/拷贝这类有风控的源惩罚性降速——封面随
                    //  滚动加载 + WnCoverImage 的 503 退避重试即可，源站惩罚并发。）
                    LazyColumn(Modifier.fillMaxSize().testTag("manga_search_results"),
                    verticalArrangement = Arrangement.spacedBy(WnSpace.sm),
                    contentPadding = PaddingValues(bottom = 16.dp)) {
                    items(hits, key = { it.source + ":" + it.comicId }) { h ->
                        WnHairlineCard(
                            modifier = Modifier.testTag("manga_result_card"),
                            onClick = { onOpen(Dest.MangaDetail(h.source, h.comicId)) },
                        ) {
                            Row(Modifier.padding(WnSpace.md)) {
                                if (h.cover.isNotBlank()) {
                                    WnCoverImage(
                                        url = remoteCoverUrl(ep, h.source, h.cover),
                                        contentDescription = h.title,
                                        loader = loader,
                                        modifier = Modifier.width(64.dp).aspectRatio(0.72f),
                                    )
                                    Spacer(Modifier.width(WnSpace.md))
                                }
                                Column(Modifier.weight(1f)) {
                                    Text(h.title.ifBlank { "（无标题）" },
                                         style = MaterialTheme.typography.bodyMedium,
                                         fontWeight = FontWeight.Medium,
                                         maxLines = 2, overflow = TextOverflow.Ellipsis)
                                    Spacer(Modifier.height(WnSpace.xs))
                                    Text(
                                        listOfNotNull(
                                            h.author.takeIf { it.isNotBlank() },
                                            h.sourceName.ifBlank { h.source },
                                        ).joinToString(" · "),
                                        style = MaterialTheme.typography.labelSmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                        maxLines = 1, overflow = TextOverflow.Ellipsis,
                                    )
                                    if (h.tags.isNotEmpty()) {
                                        Text(h.tags.take(4).joinToString(" / "),
                                             style = MaterialTheme.typography.labelSmall,
                                             color = MaterialTheme.colorScheme.onSurfaceVariant,
                                             maxLines = 1, overflow = TextOverflow.Ellipsis)
                                    }
                                }
                            }
                        }
                    }
                    }
                }
                // 既没在搜、也没结果：什么都不渲染（不摆一个空的结果容器，
                // 否则"结果标签还在、里面 0 条"会让人以为结果没被清掉——实测被用例抓到）
                else -> Spacer(Modifier.fillMaxSize())
            }
        }
    }
}

/**
 * 页码按钮（网页端 `.pager-num` 的同款）：当前页高亮，点击跳到该页。
 * 抽出来是为了让 testTag 稳定（`manga_search_page_<n>`），验收可以直接点具体页码。
 */
@Composable
private fun PagerNum(p: Int, current: Int, busy: Boolean, onClick: (Int) -> Unit) {
    val active = p == current
    OutlinedButton(
        onClick = { onClick(p) },
        enabled = !busy && !active,
        modifier = Modifier.testTag("manga_search_page_$p"),
        shape = WnChipShape,
        contentPadding = PaddingValues(horizontal = 10.dp, vertical = 2.dp),
        border = BorderStroke(1.dp, if (active) WnColors.accent else WnColors.line),
        colors = ButtonDefaults.outlinedButtonColors(
            containerColor = if (active) WnColors.accent else Color.Transparent,
            contentColor = if (active) WnColors.onAccent else WnColors.ink,
            disabledContainerColor = if (active) WnColors.accent else Color.Transparent,
            disabledContentColor = if (active) WnColors.onAccent else WnColors.inkDim,
        ),
    ) {
        Text(
            p.toString(),
            style = MaterialTheme.typography.labelSmall,
            fontWeight = if (active) FontWeight.Bold else FontWeight.Normal,
        )
    }
}
