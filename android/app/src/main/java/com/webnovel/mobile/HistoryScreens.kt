@file:OptIn(ExperimentalMaterial3Api::class)

package com.webnovel.mobile

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.History
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 阅读历史（对齐 Venera 的"历史"视图）。
 *
 * 数据全部来自既有落盘文件：小说进度（/api/books 的 last_read_ts 与 read_*）
 * 与漫画历史（/api/manga/history）。本页只读不写，点条目直接回到上次阅读处。
 * 没有历史时明确说明，而不是显示空白页。
 */
@Composable
internal fun HistoryScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    onOpen: (Dest) -> Unit,
    onBack: () -> Unit,
) {
    data class Entry(val ts: Double, val title: String, val sub: String, val open: Dest)

    var entries by remember { mutableStateOf<List<Entry>>(emptyList()) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    suspend fun reload() {
        loading = true; error = null
        val nb = gateway.httpText(ep.port, "/api/books")
        val hr = gateway.httpText(ep.port, "/api/manga/history")
        if (!nb.ok && !hr.ok) {
            error = "读取历史失败：HTTP ${nb.code}/${hr.code}"
            loading = false
            return
        }
        val novels = if (nb.ok) EngineData.novels(nb.body) else emptyList()
        val manga = if (hr.ok) EngineData.mangaHistory(hr.body) else emptyList()
        entries = (novels.filter { it.started }.map {
            Entry(it.lastReadTs, it.name, it.progressLabel, Dest.Detail(it.key))
        } + manga.map {
            Entry(it.ts, it.title.ifBlank { it.comicId },
                if (it.pos.isNotBlank()) "读到 ${it.pos}" else "第 ${it.idx + 1} 话",
                Dest.MangaDetail(it.source, it.comicId))
        }).sortedByDescending { it.ts }
        loading = false
    }
    LaunchedEffect(ep) { reload() }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text("阅读历史（${entries.size}）", maxLines = 1) },
            navigationIcon = { TextButton(onClick = onBack) { Text("← 返回") } },
            actions = { TextButton(onClick = { scope.launch { reload() } }) { Text("刷新") } },
        )
    }) { pad ->
        Box(Modifier.padding(pad).fillMaxSize()) {
            when {
                loading -> Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
                error != null -> Column(Modifier.fillMaxSize().padding(WnSpace.xl), Arrangement.Center) {
                    Text(error!!, color = MaterialTheme.colorScheme.error)
                    Spacer(Modifier.height(WnSpace.md))
                    Button(onClick = { scope.launch { reload() } }) { Text("重试") }
                }
                entries.isEmpty() -> WnEmptyState(
                    icon = Icons.Outlined.History,
                    title = "还没有阅读历史",
                    body = "读过的小说与漫画会按最近阅读时间出现在这里；" +
                        "记录来自本机的阅读进度文件，卸载应用才会清除。",
                )
                else -> LazyColumn(
                    Modifier.fillMaxSize().testTag("history_list"),
                    contentPadding = PaddingValues(WnSpace.md),
                    verticalArrangement = Arrangement.spacedBy(WnSpace.sm),
                ) {
                    items(entries) { e ->
                        WnHairlineCard(onClick = { onOpen(e.open) }) {
                            Column(Modifier.padding(WnSpace.md)) {
                                Text(e.title.ifBlank { "（无标题）" },
                                    style = MaterialTheme.typography.bodyMedium,
                                    fontWeight = FontWeight.Medium,
                                    color = WnColors.ink,
                                    maxLines = 2, overflow = TextOverflow.Ellipsis)
                                Spacer(Modifier.height(WnSpace.xs))
                                Text(
                                    e.sub + " · " + timeLabel(e.ts),
                                    style = MaterialTheme.typography.labelSmall,
                                    color = WnColors.inkDim,
                                    maxLines = 1, overflow = TextOverflow.Ellipsis,
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}

private fun timeLabel(ts: Double): String {
    if (ts <= 0) return "时间未知"
    return try {
        SimpleDateFormat("MM-dd HH:mm", Locale.US).format(Date((ts * 1000).toLong()))
    } catch (t: Throwable) {
        "时间未知"
    }
}

/**
 * 阅读设置总入口（此前只有阅读器内部能改）：
 * 小说排版（字号/行距/主题）与漫画阅读方向（纵向连续 / 横向翻页），
 * 立即生效并落盘（ReaderPrefs），引擎重启或失败都不会丢。
 */
@Composable
internal fun ReaderPrefsScreen(onBack: () -> Unit) {
    val ctx = LocalContext.current
    var prefs by remember { mutableStateOf(ReaderPrefs.load(ctx)) }
    var saved by remember { mutableStateOf<String?>(null) }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text("阅读设置", maxLines = 1) },
            navigationIcon = { TextButton(onClick = onBack) { Text("← 返回") } },
        )
    }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize().padding(WnSpace.lg)) {
            WnSectionHeader("小说排版")
            Spacer(Modifier.height(WnSpace.xs))
            Text("字号 ${prefs.fontSizeSp}", style = MaterialTheme.typography.bodyMedium,
                 color = MaterialTheme.colorScheme.onSurface)
            Slider(
                value = prefs.fontSizeSp.toFloat(),
                onValueChange = {
                    prefs = prefs.copy(fontSizeSp = it.toInt()); ReaderPrefs.save(ctx, prefs)
                },
                valueRange = ReaderPrefs.MIN_FONT.toFloat()..ReaderPrefs.MAX_FONT.toFloat(),
                steps = ReaderPrefs.MAX_FONT - ReaderPrefs.MIN_FONT - 1,
                modifier = Modifier.testTag("prefs_font"),
            )
            Text("行距 " + String.format(Locale.US, "%.1f", prefs.lineHeightMul),
                 style = MaterialTheme.typography.bodyMedium,
                 color = MaterialTheme.colorScheme.onSurface)
            Slider(
                value = prefs.lineHeightMul,
                onValueChange = {
                    prefs = prefs.copy(lineHeightMul = (it * 10).toInt() / 10f)
                    ReaderPrefs.save(ctx, prefs)
                },
                valueRange = ReaderPrefs.MIN_LINE..ReaderPrefs.MAX_LINE,
            )
            Spacer(Modifier.height(WnSpace.md))
            Text("主题", style = MaterialTheme.typography.bodyMedium,
                 color = MaterialTheme.colorScheme.onSurface)
            Spacer(Modifier.height(WnSpace.xs))
            Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                ReaderTheme.entries.forEach { t ->
                    FilterChip(
                        selected = prefs.theme == t,
                        onClick = { prefs = prefs.copy(theme = t); ReaderPrefs.save(ctx, prefs) },
                        label = { Text(t.label) },
                        shape = WnPillShape,
                        colors = FilterChipDefaults.filterChipColors(
                            containerColor = WnColors.surface,
                            labelColor = WnColors.inkDim,
                            selectedContainerColor = WnColors.accent,
                            selectedLabelColor = WnColors.onAccent,
                        ),
                        border = FilterChipDefaults.filterChipBorder(
                            enabled = true,
                            selected = prefs.theme == t,
                            borderColor = WnColors.line,
                            selectedBorderColor = WnColors.accent,
                        ),
                    )
                }
            }
            Spacer(Modifier.height(WnSpace.xl))
            WnSectionHeader("漫画阅读方向")
            Text(
                if (prefs.horizontalPaging) "当前：横向翻页（一屏一页）" else "当前：纵向连续（上下滚动）",
                style = MaterialTheme.typography.bodySmall, color = WnColors.inkDim,
            )
            Spacer(Modifier.height(WnSpace.sm))
            Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                if (!prefs.horizontalPaging) {
                    Button(onClick = {
                        prefs = prefs.copy(horizontalPaging = false); ReaderPrefs.save(ctx, prefs)
                        saved = "已设为纵向连续"
                    }, shape = WnPillShape) { Text("纵向连续") }
                    OutlinedButton(onClick = {
                        prefs = prefs.copy(horizontalPaging = true); ReaderPrefs.save(ctx, prefs)
                        saved = "已设为横向翻页"
                    }, shape = WnPillShape) { Text("横向翻页") }
                } else {
                    OutlinedButton(onClick = {
                        prefs = prefs.copy(horizontalPaging = false); ReaderPrefs.save(ctx, prefs)
                        saved = "已设为纵向连续"
                    }, shape = WnPillShape) { Text("纵向连续") }
                    Button(onClick = {
                        prefs = prefs.copy(horizontalPaging = true); ReaderPrefs.save(ctx, prefs)
                        saved = "已设为横向翻页"
                    }, shape = WnPillShape) { Text("横向翻页") }
                }
            }
            saved?.let {
                Spacer(Modifier.height(WnSpace.sm))
                Text(it, style = MaterialTheme.typography.labelMedium,
                     color = WnColors.accent)
            }
            Spacer(Modifier.height(WnSpace.lg))
            Text(
                "漫画阅读器里也能直接切换（顶栏『翻页/连续』），两边共用同一份偏好；" +
                    "小说阅读器里的字号/行距/主题与本页也是同一份设置。",
                style = MaterialTheme.typography.labelSmall,
                color = WnColors.inkDim,
            )
        }
    }
}
