@file:OptIn(ExperimentalMaterial3Api::class)

package com.webnovel.mobile

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
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
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * **存储管理（原生）**：按源/作品/章节看占用，并做**选择性清理**（路线 §7 P1-2）。
 *
 * 三条纪律（与 server/storage.py 同一口径）：
 *   1. 数字全部来自服务端磁盘实扫，客户端不估算、不四舍五入到 MB 糊弄；
 *   2. "已下载内容"（小说正文、漫画离线图片）标注为**用户数据**，只在用户明确点名时删，
 *      清理前必须二次确认，确认框里写清"删什么/不删什么"；
 *   3. 清理结果照实显示：释放多少字节（服务端实测）、有没有删不掉的（kept 原因）。
 */
@Composable
internal fun StorageScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    onBack: () -> Unit,
) {
    var usage by remember { mutableStateOf<StorageUsage?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var msg by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var confirm by remember { mutableStateOf<Pair<String, JSONObject>?>(null) }
    var expandedBook by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    // ── 图片缓存（客户端 Coil 缓存：封面 + 在线阅读页；可调上限、可清除）──
    val ctx = LocalContext.current
    var imgCacheBytes by remember { mutableLongStateOf(-1L) }   // -1 = 统计中
    var imgCacheLimit by remember { mutableIntStateOf(imageCacheLimitMb(ctx)) }
    var imgConfirmClear by remember { mutableStateOf(false) }
    fun reloadImgCache() {
        scope.launch {
            imgCacheBytes = withContext(Dispatchers.IO) { imageCacheSizeBytes(ctx) }
        }
    }
    LaunchedEffect(ep) { reloadImgCache() }

    suspend fun refresh() {
        val r = gateway.httpText(ep.port, "/api/storage")
        if (r.ok) {
            usage = EngineData.storageUsage(r.body)
            error = null
        } else {
            error = "读取存储占用失败：HTTP ${r.code}"
        }
        loading = false
    }

    /** 真正执行清理：结果按服务端返回的实测值显示（不自己算） */
    suspend fun doClear(body: JSONObject) {
        busy = true
        val r = gateway.httpPost(ep.port, "/api/storage/clear", body.toString())
        val o = runCatching { JSONObject(r.body) }.getOrNull()
        msg = if (r.ok && o?.optBoolean("ok") == true) {
            val freed = o.optLong("freed_bytes", 0)
            val del = o.optJSONArray("deleted")?.length() ?: 0
            val kept = o.optJSONArray("kept")
            buildString {
                append("已清理 ").append(del).append(" 项，释放 ").append(Export.human(freed))
                if (freed == 0L && del > 0) append("（删掉的是空/损坏缓存，本身不占空间）")
                if (kept != null && kept.length() > 0) {
                    append("；有 ").append(kept.length()).append(" 项没删掉：")
                    append(kept.optJSONObject(0)?.optString("reason").orEmpty().take(60))
                }
            }
        } else {
            "清理失败：HTTP ${r.code} · " +
                (o?.optString("error")?.take(80) ?: r.body.take(80))
        }
        busy = false
        refresh()
    }

    LaunchedEffect(ep) { refresh() }

    // 清理结果消息**不放在 LazyColumn 里**：清理按钮在列表深处，滚下去点完后若结果
    // 在列表顶部，用户（与测试）都看不见——实测踩到：点了"清理"却没有结果提示。
    Scaffold(topBar = {
        TopAppBar(
            title = { Text("存储管理", maxLines = 1) },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                }
            },
            actions = {
                TextButton(onClick = { scope.launch { refresh() } }, enabled = !busy) {
                    Text("刷新")
                }
            },
        )
    }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize()) {
        msg?.let {
            Text(
                it,
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.primary,
                modifier = Modifier.padding(horizontal = WnSpace.md, vertical = 2.dp)
                    .testTag("storage_msg"),
            )
        }
        LazyColumn(
            Modifier.fillMaxWidth().weight(1f).testTag("storage_list"),
            contentPadding = PaddingValues(WnSpace.md),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {

        when {
            loading -> item {
                Box(Modifier.fillMaxWidth().padding(WnSpace.xl), Alignment.Center) {
                    CircularProgressIndicator()
                }
            }
            error != null -> item {
                Column(Modifier.fillMaxWidth().padding(WnSpace.lg)) {
                    Text(error!!, color = MaterialTheme.colorScheme.error)
                    Spacer(Modifier.height(WnSpace.sm))
                    Button(onClick = { scope.launch { refresh() } }) { Text("重试") }
                }
            }
            usage == null -> item {
                Text("服务端没有返回占用数据", Modifier.padding(WnSpace.lg))
            }
            else -> {
                val u = usage!!
                item {
                    WnHairlineCard {
                        Column(Modifier.padding(WnSpace.md)) {
                            Text("本机共占用 ${Export.human(u.totalBytes)}",
                                style = MaterialTheme.typography.titleMedium,
                                modifier = Modifier.testTag("storage_total"))
                            Spacer(Modifier.height(WnSpace.xs))
                            Text("统计时间：${u.generatedAt}（数字来自磁盘实扫）",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.outline)
                        }
                    }
                }
                item {
                    // 图片缓存管理（客户端缓存，不在服务端 /api/storage 统计内）
                    WnHairlineCard {
                        Column(Modifier.padding(WnSpace.md)) {
                            Text("图片缓存（封面与在线阅读页）",
                                style = MaterialTheme.typography.titleSmall,
                                fontWeight = FontWeight.SemiBold)
                            Spacer(Modifier.height(WnSpace.xs))
                            Text(
                                "缓存大小：" +
                                    if (imgCacheBytes >= 0) Export.human(imgCacheBytes)
                                    else "统计中…",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.outline,
                                modifier = Modifier.testTag("img_cache_size"))
                            Spacer(Modifier.height(WnSpace.sm))
                            Row(Modifier.fillMaxWidth(),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.SpaceBetween) {
                                Text("缓存限制：" +
                                    (if (imgCacheLimit <= 0) "不限" else "$imgCacheLimit MB"),
                                    style = MaterialTheme.typography.bodySmall)
                                Box {
                                    var menu by remember { mutableStateOf(false) }
                                    OutlinedButton(onClick = { menu = true },
                                        modifier = Modifier.testTag("img_cache_limit")) {
                                        Text("设置")
                                    }
                                    DropdownMenu(expanded = menu,
                                        onDismissRequest = { menu = false }) {
                                        listOf(256, 512, 1024, 2048, 4096, 0).forEach { mb ->
                                            DropdownMenuItem(
                                                text = { Text(if (mb <= 0) "不限" else "$mb MB") },
                                                onClick = {
                                                    menu = false
                                                    imgCacheLimit = mb
                                                    setImageCacheLimitMb(ctx, mb)
                                                    msg = "缓存上限已设置为" +
                                                        (if (mb <= 0) "不限" else " $mb MB") +
                                                        "，下次启动应用后生效"
                                                })
                                        }
                                    }
                                }
                            }
                            Spacer(Modifier.height(WnSpace.xs))
                            Text("手机端不限制缓存占用；上限只决定自动淘汰的起点。" +
                                "清除不影响已下载的正文与漫画图片。",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.outline)
                            Spacer(Modifier.height(WnSpace.sm))
                            OutlinedButton(onClick = { imgConfirmClear = true },
                                modifier = Modifier.testTag("img_cache_clear")) {
                                Text("清除图片缓存")
                            }
                        }
                    }
                }
                item {
                    Text(
                        "已下载的小说正文与漫画图片是你的数据：**只有你明确点名才会删**。" +
                            "清理缓存不会影响它们。",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                    )
                }
                // 分类总览 + 各分类的清理入口
                items(u.categories, key = { it.key }) { c ->
                    WnHairlineCard {
                        Column(Modifier.padding(WnSpace.md)) {
                            Row {
                                Text(c.label, style = MaterialTheme.typography.bodyMedium)
                                Spacer(Modifier.weight(1f))
                                Text(Export.human(c.bytes),
                                    style = MaterialTheme.typography.bodyMedium,
                                    modifier = Modifier.testTag("cat_${c.key}"))
                            }
                            if (c.note.isNotBlank()) {
                                Text(c.note, style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.outline)
                            }
                            Spacer(Modifier.height(6.dp))
                            Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                                if (c.key == "regen") {
                                    OutlinedButton(
                                        enabled = !busy,
                                        modifier = Modifier.testTag("clear_regen"),
                                        onClick = {
                                            confirm = "清理可再生数据（搜索/目录缓存、日志）？" +
                                                "已下载内容不受影响。" to
                                                JSONObject().put("scope", "regen")
                                        },
                                    ) { Text("清理") }
                                }
                                if (c.key == "manga_cache") {
                                    OutlinedButton(
                                        enabled = !busy,
                                        modifier = Modifier.testTag("clear_manga_cache"),
                                        onClick = {
                                            confirm = "清理漫画临时缓存？" +
                                                "离线已下载的图片不会动。" to
                                                JSONObject().put("scope", "manga_cache")
                                        },
                                    ) { Text("清理") }
                                }
                                if (c.key == "trash" && c.bytes > 0) {
                                    OutlinedButton(
                                        enabled = !busy,
                                        modifier = Modifier.testTag("clear_trash"),
                                        onClick = {
                                            confirm = "清空回收站？清空后应用内无法恢复。" +
                                                "正文与图片不受影响。" to
                                                JSONObject().put("scope", "trash")
                                        },
                                    ) { Text("清空") }
                                }
                            }
                        }
                    }
                }

                // 小说：逐书占用 + 章节级清理（只清"错误缓存"）
                if (u.novelBooks.isNotEmpty()) {
                    item { Text("小说（${u.novelBooks.size} 本）",
                        style = MaterialTheme.typography.titleSmall) }
                    items(u.novelBooks, key = { "book_" + it.key }) { b ->
                        WnHairlineCard {
                            Column(Modifier.padding(WnSpace.md)) {
                                Text(b.name, style = MaterialTheme.typography.bodyMedium,
                                    maxLines = 1, overflow = TextOverflow.Ellipsis)
                                Text(
                                    "已缓存 ${b.cached}/${b.total} 章" +
                                        (if (b.failed > 0) " · 失败 ${b.failed} 章" else "") +
                                        " · ${Export.human(b.bytes)}",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.outline,
                                )
                                if (b.stopReason.isNotBlank()) {
                                    Text(b.stopReason, style = MaterialTheme.typography.labelSmall,
                                        color = MaterialTheme.colorScheme.outline)
                                }
                                Spacer(Modifier.height(6.dp))
                                Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                                    if (b.failed > 0 || b.cached < b.total) {
                                        OutlinedButton(
                                            enabled = !busy,
                                            modifier = Modifier.testTag("clear_failed_${b.key}"),
                                            onClick = {
                                                confirm = "清理《${b.name}》的错误缓存" +
                                                    "（抓取失败的章节与内容为空的缓存）？" +
                                                    "清理后可在下载页点「继续」重新抓取。" to JSONObject()
                                                    .put("scope", "book_chapters")
                                                    .put("key", b.key).put("only", "failed")
                                            },
                                        ) { Text("清理错误缓存") }
                                    }
                                    OutlinedButton(
                                        enabled = !busy,
                                        modifier = Modifier.testTag("toggle_book_${b.key}"),
                                        onClick = {
                                            expandedBook = if (expandedBook == b.key) null else b.key
                                        },
                                    ) { Text(if (expandedBook == b.key) "收起章节" else "按章节清理") }
                                }
                                if (expandedBook == b.key) {
                                    Spacer(Modifier.height(6.dp))
                                    Text(
                                        "已缓存章节：${b.cachedIndexes.joinToString("、").ifBlank { "无" }}" +
                                            "（点某一章只删那一章的缓存）",
                                        style = MaterialTheme.typography.labelSmall,
                                        color = MaterialTheme.colorScheme.outline,
                                    )
                                    Spacer(Modifier.height(WnSpace.xs))
                                    // 章节按钮按 10 个一批换行，避免长目录把界面撑爆
                                    b.cachedIndexes.chunked(10).forEach { row ->
                                        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                                            row.forEach { idx ->
                                                OutlinedButton(
                                                    enabled = !busy,
                                                    contentPadding = PaddingValues(
                                                        horizontal = WnSpace.sm, vertical = WnSpace.xs),
                                                    modifier = Modifier
                                                        .testTag("clear_chapter_${b.key}_$idx"),
                                                    onClick = {
                                                        confirm = "删除《${b.name}》第 $idx 章的缓存？" +
                                                            "删除后需重新联网抓取。" to JSONObject()
                                                            .put("scope", "book_chapters")
                                                            .put("key", b.key)
                                                            .put("chapters",
                                                                org.json.JSONArray().put(idx))
                                                    },
                                                ) { Text("第 $idx 章") }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                // 漫画：逐源临时缓存 + 逐作品已下载（删除已下载走书库移除接口，这里只展示）
                if (u.mangaCache.isNotEmpty()) {
                    item { Text("漫画临时缓存（按源）",
                        style = MaterialTheme.typography.titleSmall) }
                    items(u.mangaCache, key = { "mc_" + it.source }) { m ->
                        WnHairlineCard {
                            Row(Modifier.padding(WnSpace.md), verticalAlignment = Alignment.CenterVertically) {
                                Column(Modifier.weight(1f)) {
                                    Text(m.source, style = MaterialTheme.typography.bodyMedium)
                                    Text("${m.works} 个作品 · ${Export.human(m.bytes)}",
                                        style = MaterialTheme.typography.labelSmall,
                                        color = MaterialTheme.colorScheme.outline)
                                }
                                OutlinedButton(
                                    enabled = !busy,
                                    onClick = {
                                        confirm = "清理「${m.source}」的临时缓存？" +
                                            "已下载的离线图片不会动。" to JSONObject()
                                            .put("scope", "manga_cache").put("source", m.source)
                                    },
                                ) { Text("清理") }
                            }
                        }
                    }
                }
                if (u.mangaDownloads.isNotEmpty()) {
                    item { Text("漫画已下载（${u.mangaDownloads.size} 部，离线阅读用）",
                        style = MaterialTheme.typography.titleSmall) }
                    items(u.mangaDownloads, key = { "md_" + it.source + ":" + it.comicId }) { m ->
                        WnHairlineCard {
                            Column(Modifier.padding(WnSpace.md)) {
                                Text(m.title, style = MaterialTheme.typography.bodyMedium,
                                    maxLines = 1, overflow = TextOverflow.Ellipsis)
                                Text("${m.source} · ${m.images} 张 · ${Export.human(m.bytes)}",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.outline)
                                Spacer(Modifier.height(WnSpace.xs))
                                Text("删除已下载图片请到书库移除该作品（那里会问要不要连文件一起删）",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.outline)
                            }
                        }
                    }
                }
                if (u.trashItems.isNotEmpty()) {
                    item { Text("回收站（${u.trashItems.size} 项，可清空）",
                        style = MaterialTheme.typography.titleSmall) }
                    items(u.trashItems.take(10), key = { "t_" + it.name }) { t ->
                        Row(Modifier.fillMaxWidth().padding(horizontal = WnSpace.md, vertical = 2.dp)) {
                            Text(t.name, style = MaterialTheme.typography.labelSmall,
                                maxLines = 1, overflow = TextOverflow.Ellipsis,
                                modifier = Modifier.weight(1f))
                            Text(Export.human(t.bytes), style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.outline)
                        }
                    }
                }
            }
        }
        }
        }
    }

    confirm?.let { (text, body) ->
        AlertDialog(
            onDismissRequest = { confirm = null },
            // 确认按钮单独打 tag：列表里也有多个「清理」按钮，按文本点会点错
            // （实测踩到：点在弹窗后面的列表按钮上，清理根本没执行）
            modifier = Modifier.testTag("storage_confirm_dialog"),
            title = { Text("确认清理") },
            text = { Text(text) },
            confirmButton = {
                TextButton(
                    onClick = {
                        confirm = null
                        scope.launch { doClear(body) }
                    },
                    modifier = Modifier.testTag("storage_confirm"),
                ) { Text("清理") }
            },
            dismissButton = { TextButton(onClick = { confirm = null }) { Text("取消") } },
        )
    }

    if (imgConfirmClear) {
        AlertDialog(
            onDismissRequest = { imgConfirmClear = false },
            title = { Text("清除图片缓存？") },
            text = {
                Text("删除全部封面与在线阅读页缓存（当前约 " +
                    (if (imgCacheBytes >= 0) Export.human(imgCacheBytes) else "未知") +
                    "）。不影响已下载的小说正文与漫画图片；之后浏览时会按需重新加载。")
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        imgConfirmClear = false
                        scope.launch {
                            val before = withContext(Dispatchers.IO) {
                                imageCacheSizeBytes(ctx)
                            }
                            clearImageCache(ctx)
                            reloadImgCache()
                            msg = "已清除图片缓存，释放 ${Export.human(before)}"
                        }
                    },
                    modifier = Modifier.testTag("img_cache_clear_confirm"),
                ) { Text("清除") }
            },
            dismissButton = {
                TextButton(onClick = { imgConfirmClear = false }) { Text("取消") }
            },
        )
    }
}
