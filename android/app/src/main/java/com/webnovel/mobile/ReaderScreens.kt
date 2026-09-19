@file:OptIn(ExperimentalMaterial3Api::class)

package com.webnovel.mobile

import android.net.Uri
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.outlined.TextFields
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch
import org.json.JSONObject

// ── 主题配色：正文与纸张成对定义 ──────────────────────────────

private data class ReaderColors(val bg: Color, val fg: Color, val dim: Color, val bar: Color)

private fun colorsFor(t: ReaderTheme): ReaderColors = when (t) {
    ReaderTheme.Light -> ReaderColors(Color(0xFFFFFFFF), Color(0xFF1A1A1A), Color(0xFF8A8A8A), Color(0xFFF2F2F2))
    ReaderTheme.Paper -> ReaderColors(Color(0xFFF5EFE0), Color(0xFF3A3325), Color(0xFF8C8271), Color(0xFFEAE2CF))
    ReaderTheme.Dark -> ReaderColors(Color(0xFF23221F), Color(0xFFE8E6DD), Color(0xFF8A8A80), Color(0xFF2C2B27))
}

/** 书籍相关路径统一编码：书籍 key 是中文目录名，必须转义后才能进 URL */
private fun bookPath(key: String, suffix: String = ""): String =
    "/api/books/${Uri.encode(key)}$suffix"

// ── 详情页（原生，替代 WebView 过渡）────────────────────────

@Composable
fun BookDetailScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    bookKey: String,
    onBack: () -> Unit,
    onRead: (Int) -> Unit,
) {
    val ctx = androidx.compose.ui.platform.LocalContext.current
    var detail by remember { mutableStateOf<BookDetail?>(null) }
    // 阅读位置来自全局进度端点（详情里的 progress 是抓取任务进度，不是阅读位置）
    var progress by remember { mutableStateOf<ReadProgress?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var introExpanded by remember { mutableStateOf(false) }
    var exportMsg by remember { mutableStateOf<String?>(null) }
    var exporting by remember { mutableStateOf(false) }
    var checkMsg by remember { mutableStateOf<String?>(null) }
    var checking by remember { mutableStateOf(false) }
    // 净化预检结果（dry-run）：确认前不写盘
    var cleaning by remember { mutableStateOf(false) }
    var cleanPreview by remember { mutableStateOf<org.json.JSONObject?>(null) }
    var missingCount by remember { mutableIntStateOf(0) }
    var confirmDelete by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    // 通过系统文件选择器（SAF）导出全文：内容由本机引擎提供，流式写入选定位置
    val exportLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.CreateDocument("text/plain")
    ) { uri ->
        if (uri == null) {
            exportMsg = "已取消导出"
            return@rememberLauncherForActivityResult
        }
        scope.launch {
            exporting = true; exportMsg = "正在导出…"
            exportMsg = try {
                val r = Export.streamToUri(ctx, ep.port, ep.token, bookPath(bookKey, "/txt"), uri)
                "已导出 ${Export.human(r.bytes)}" + (r.warning?.let { "（$it）" } ?: "")
            } catch (t: kotlinx.coroutines.CancellationException) {
                throw t
            } catch (t: Throwable) {
                "导出失败：${t.message ?: t.javaClass.simpleName}"
            }
            exporting = false
        }
    }

    fun reload() {
        scope.launch {
            loading = true; error = null
            val r = gateway.httpText(ep.port, bookPath(bookKey))
            val d = if (r.ok) EngineData.bookDetail(r.body) else null
            if (d == null) {
                error = "读取目录失败：HTTP ${r.code}${if (r.body.isNotBlank()) " · ${r.body.take(120)}" else ""}"
            } else {
                detail = d
                val pr = gateway.httpText(ep.port, bookPath(bookKey, "/progress"))
                progress = if (pr.ok) EngineData.readProgress(pr.body) else null
            }
            loading = false
        }
    }
    LaunchedEffect(bookKey) { reload() }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text(detail?.name ?: "书籍详情", maxLines = 1, overflow = TextOverflow.Ellipsis) },
            navigationIcon = { TextButton(onClick = onBack) { Text("← 返回") } },
        )
    }) { pad ->
        val d = detail
        Box(Modifier.padding(pad).fillMaxSize()) {
            when {
                loading -> Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
                d == null -> Column(Modifier.fillMaxSize().padding(WnSpace.xl), Arrangement.Center) {
                    Text(error ?: "书籍不存在", color = MaterialTheme.colorScheme.error)
                    Spacer(Modifier.height(WnSpace.md))
                    Button(onClick = { reload() }) { Text("重试") }
                }
                else -> LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = WnSpace.xl)) {
                    item { DetailHeader(d, progress, introExpanded) { introExpanded = !introExpanded } }
                    item {
                        val resume = d.resumeIndex(progress)
                        WnHairlineCard(Modifier.padding(horizontal = WnSpace.lg)) {
                            Column(Modifier.padding(WnSpace.md)) {
                                Button(
                                    onClick = { onRead(resume) },
                                    modifier = Modifier.fillMaxWidth(),
                                    shape = WnPillShape,
                                ) {
                                    Text(if (progress?.started == true) "继续阅读 第 $resume 章" else "开始阅读")
                                }
                                Spacer(Modifier.height(WnSpace.sm))
                                Row(Modifier.fillMaxWidth(),
                                    horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                                    OutlinedButton(
                                        onClick = { exportLauncher.launch(Export.safeFileName(d.name, "book") + ".txt") },
                                        enabled = !exporting,
                                        modifier = Modifier.weight(1f),
                                    ) { Text(if (exporting) "导出中…" else "导出 TXT") }
                                    OutlinedButton(
                                        onClick = { confirmDelete = true },
                                        modifier = Modifier.weight(1f),
                                    ) { Text("从书架删除") }
                                    OutlinedButton(
                                        onClick = {
                                            scope.launch {
                                                checking = true
                                                checkMsg = "正在检查更新…"
                                                // 检查是**异步**的：先 202，再轮询 check-status（与网页端一致）
                                                val start = gateway.httpPost(
                                                    ep.port, bookPath(bookKey, "/check-update"))
                                                if (!start.ok && start.code != 202) {
                                                    checkMsg = "检查更新失败：HTTP ${start.code}"
                                                } else {
                                                    // 检查更新本身**不建任务**（只有"下载缺失章节"才建），
                                                    // 所以超时不能说"可在下载页看任务"——那是另一件事。
                                                    // 实测慢源（分页目录 60+ 页）单次检查可到 20s 左右。
                                                    var msg = "检查仍在进行（源站较慢），稍后再点一次「检查更新」查看结果"
                                                    for (i in 0 until 20) {
                                                        delay(1500)
                                                        val st = gateway.httpText(
                                                            ep.port, bookPath(bookKey, "/check-status"))
                                                        // 文案与缺失数口径的唯一来源是
                                                        // EngineData.novelUpdateCheck（可被用例钉住）：
                                                        //  · 缺失数用服务端的 missing_count；
                                                        //  · "目录未取全/源站不可达"如实透出；
                                                        //  · 老服务端缺字段时回退 new+failed。
                                                        val chk = EngineData.novelUpdateCheck(st.body)
                                                        // 仍在检查就继续轮询（慢源目录 60+ 页，实测可到 20s）
                                                        if (chk.status == "checking") continue
                                                        missingCount = chk.missingCount
                                                        msg = chk.label
                                                        break
                                                    }
                                                    checkMsg = msg
                                                }
                                                checking = false
                                            }
                                        },
                                        enabled = !checking,
                                        modifier = Modifier.weight(1f),
                                    ) { Text(if (checking) "检查中…" else "检查更新") }
                                    OutlinedButton(
                                        onClick = {
                                            scope.launch {
                                                cleaning = true; checkMsg = "正在统计需要净化的章节…"
                                                // 先 dry-run：把"会删多少"摆出来再让用户确认，
                                                // 绝不一声不响就改已下载的正文
                                                val r = gateway.httpPost(
                                                    ep.port, bookPath(bookKey, "/reclean?dry_run=1"))
                                                val o = runCatching {
                                                    org.json.JSONObject(r.body)
                                                }.getOrNull()
                                                cleaning = false
                                                if (!r.ok || o == null) {
                                                    checkMsg = "净化预检失败：HTTP ${r.code}"
                                                } else {
                                                    cleanPreview = o
                                                    val ch = o.optInt("changed")
                                                    val lines = o.optInt("removed_lines")
                                                    val chars = o.optInt("removed_chars")
                                                    checkMsg = if (ch == 0) {
                                                        "已下载正文不需要净化（没有检测到广告或分隔线）"
                                                    } else {
                                                        "预检：$ch 章需要净化，将删除 $lines 行垃圾、" +
                                                            "约 $chars 字（作者正文不会动）"
                                                    }
                                                }
                                            }
                                        },
                                        enabled = !cleaning && !checking,
                                        modifier = Modifier
                                            .weight(1f)
                                            .testTag("reclean_button"),
                                    ) { Text(if (cleaning) "统计中…" else "净化正文") }
                                }
                                if (missingCount > 0) {
                                    Spacer(Modifier.height(WnSpace.sm))
                                    OutlinedButton(
                                        onClick = {
                                            scope.launch {
                                                // 缺失章节由服务端登记，这里只提交"按缺失清单增量下载"
                                                val r = gateway.httpPost(
                                                    ep.port, bookPath(bookKey, "/download-missing"))
                                                checkMsg = if (r.ok) {
                                                    "已创建增量下载任务（见下载页）"
                                                } else {
                                                    "创建失败：HTTP ${r.code} ${r.body.take(80)}"
                                                }
                                            }
                                        },
                                        modifier = Modifier.fillMaxWidth(),
                                    ) { Text("下载缺失章节（$missingCount）") }
                                }
                                exportMsg?.let {
                                    Spacer(Modifier.height(WnSpace.sm))
                                    Text(it, style = MaterialTheme.typography.labelMedium,
                                         color = WnColors.accent)
                                }
                                checkMsg?.let {
                                    Spacer(Modifier.height(WnSpace.xs))
                                    Text(it, style = MaterialTheme.typography.labelMedium,
                                         color = WnColors.inkDim)
                                }
                                Spacer(Modifier.height(WnSpace.sm))
                                HorizontalDivider(color = WnColors.line)
                                Spacer(Modifier.height(WnSpace.sm))
                                Text(
                                    buildString {
                                        append("目录 ").append(d.total).append(" 章 · 已下载 ")
                                            .append(d.done).append(" 章")
                                        if (d.failedCount > 0) append(" · 失败 ").append(d.failedCount).append(" 章")
                                        if (d.crawlStatus == "running") {
                                            append(" · 抓取中 ").append(d.crawlCompleted)
                                                .append("/").append(d.crawlTotal)
                                        }
                                    },
                                    style = MaterialTheme.typography.bodySmall,
                                    color = WnColors.inkDim,
                                )
                            }
                        }
                        Spacer(Modifier.height(WnSpace.md))
                    }
                    items(d.chapters, key = { it.index }) { c ->
                        val readIdx = progress?.idx ?: 0
                        ChapterRow(c, isCurrent = c.index == readIdx,
                            isRead = readIdx >= 1 && c.index < readIdx) { onRead(c.index) }
                    }
                }
            }
        }
    }
    cleanPreview?.let { o ->
        AlertDialog(
            onDismissRequest = { cleanPreview = null },
            title = { Text("净化已下载正文？") },
            text = {
                Column {
                    Text(
                        "将重新净化 ${o.optInt("chapters")} 章里的 ${o.optInt("changed")} 章，" +
                            "删除 ${o.optInt("removed_lines")} 行广告/分隔线与约 " +
                            "${o.optInt("removed_chars")} 个字符，并重建全文导出。",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    val samples = o.optJSONArray("samples")
                    if (samples != null && samples.length() > 0) {
                        Spacer(Modifier.height(6.dp))
                        Text("将删除的内容样例：", style = MaterialTheme.typography.labelSmall)
                        for (i in 0 until samples.length()) {
                            Text("· " + samples.optString(i).take(60),
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.outline)
                        }
                    }
                    Spacer(Modifier.height(6.dp))
                    Text("正文段落、章节标题、阅读进度都不会动；已下载的书不需要重下。",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline)
                }
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        cleanPreview = null
                        scope.launch {
                            cleaning = true; checkMsg = "正在净化已下载正文…"
                            val r = gateway.httpPost(ep.port,
                                bookPath(bookKey, "/reclean"), "{\"dry_run\":false}")
                            val res = runCatching { org.json.JSONObject(r.body) }.getOrNull()
                            cleaning = false
                            checkMsg = if (!r.ok || res == null) {
                                "净化失败：HTTP ${r.code} ${r.body.take(80)}"
                            } else {
                                "已净化 ${res.optInt("changed")} 章（删除 " +
                                    "${res.optInt("removed_lines")} 行 / ${res.optInt("removed_chars")} 字）" +
                                    (if (res.optBoolean("txt_rebuilt")) "；全文导出已重建" else "") +
                                    (if (res.optJSONArray("failed")?.length() ?: 0 > 0)
                                        "；${res.optJSONArray("failed")!!.length()} 章失败（详见日志）" else "")
                            }
                            reload()
                        }
                    },
                    modifier = Modifier.testTag("reclean_confirm"),
                ) { Text("净化") }
            },
            dismissButton = { TextButton(onClick = { cleanPreview = null }) { Text("取消") } },
        )
    }
    if (confirmDelete) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            title = { Text("从书架删除？") },
            text = {
                Text(
                    "书目录会被移入本机 data/trash（可人工恢复），阅读进度与已下载章节缓存都不再出现在书架里。" +
                        "此操作不会联网。"
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmDelete = false
                    scope.launch {
                        val r = gateway.httpDelete(ep.port, bookPath(bookKey))
                        exportMsg = if (r.ok) "已从书架删除：${detail?.name ?: bookKey}"
                                    else "删除失败：HTTP ${r.code} ${r.body.take(80)}"
                        if (r.ok) onBack()
                    }
                }) { Text("删除") }
            },
            dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text("取消") } },
        )
    }
}

@Composable
private fun DetailHeader(d: BookDetail, p: ReadProgress?, expanded: Boolean, onToggleIntro: () -> Unit) {
    Column(Modifier.fillMaxWidth().padding(horizontal = WnSpace.lg, vertical = WnSpace.md)) {
        Text(
            d.name,
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.Bold,
            color = WnColors.ink,
        )
        Spacer(Modifier.height(WnSpace.xs))
        Text(
            buildString {
                append(d.author.ifBlank { "佚名" })
                append(" · 共 ").append(d.total).append(" 章")
                if (p != null && p.started) append(" · 读到第 ").append(p.idx).append(" 章")
            },
            style = MaterialTheme.typography.bodySmall,
            color = WnColors.inkDim,
        )
        if (d.intro.isNotBlank()) {
            Spacer(Modifier.height(WnSpace.sm))
            Text(
                d.intro,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurface,
                maxLines = if (expanded) Int.MAX_VALUE else 5,
                overflow = TextOverflow.Ellipsis,
            )
            TextButton(onClick = onToggleIntro, contentPadding = PaddingValues(0.dp)) {
                Text(if (expanded) "收起简介" else "展开简介", style = MaterialTheme.typography.labelMedium)
            }
        }
        Spacer(Modifier.height(WnSpace.xs))
    }
}

@Composable
private fun ChapterRow(c: ChapterItem, isCurrent: Boolean, isRead: Boolean, onClick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().clickable { onClick() }
            .padding(horizontal = WnSpace.lg, vertical = WnSpace.md),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            "${c.index}",
            style = MaterialTheme.typography.labelMedium,
            color = WnColors.inkDim,
            modifier = Modifier.width(44.dp),
        )
        Text(
            c.label,
            style = MaterialTheme.typography.bodyMedium,
            color = WnColors.ink,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
        )
        val tag = when {
            isCurrent -> "在读"
            c.failed -> "失败"
            c.downloaded -> "已下载"
            isRead -> "已读"
            else -> "未下载"
        }
        Text(
            tag,
            style = MaterialTheme.typography.labelSmall,
            color = when {
                isCurrent -> WnColors.accent
                c.failed -> WnColors.danger
                else -> WnColors.inkDim
            },
        )
    }
    HorizontalDivider(color = WnColors.line)
}

// ── 原生小说阅读器 ───────────────────────────────────────────

@Composable
fun NovelReaderScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    bookKey: String,
    startIndex: Int,
    startPct: Int,
    onBack: () -> Unit,
) {
    val ctx = androidx.compose.ui.platform.LocalContext.current
    var prefs by remember { mutableStateOf(ReaderPrefs.load(ctx)) }
    var index by remember { mutableIntStateOf(startIndex.coerceAtLeast(1)) }
    var chapter by remember { mutableStateOf<ChapterContent?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var crawling by remember { mutableStateOf(false) }
    var crawlMsg by remember { mutableStateOf<String?>(null) }
    var showToc by remember { mutableStateOf(false) }
    var showPrefs by remember { mutableStateOf(false) }
    var posPct by remember { mutableIntStateOf(startPct) }
    val cache = remember { mutableStateMapOf<Int, ChapterContent>() }
    val scope = rememberCoroutineScope()
    val bg = colorsFor(prefs.theme)

    /** 读一章：先用内存缓存，未命中再请求；正文只有 downloaded=true 才作数 */
    suspend fun load(idx: Int) {
        loading = true; error = null; crawlMsg = null
        val hit = cache[idx]
        if (hit != null) {
            chapter = hit; index = idx; loading = false
            return
        }
        val r = gateway.httpText(ep.port, bookPath(bookKey, "/chapter/$idx"))
        val c = if (r.ok) EngineData.chapter(r.body) else null
        if (c == null) {
            error = "加载失败：HTTP ${r.code}" + if (r.body.isNotBlank()) " · ${r.body.take(120)}" else ""
        } else {
            chapter = c; index = c.index.coerceAtLeast(idx)
            // 章内位置：只有恢复目标章沿用传入的百分比，换章从章首开始
            posPct = if (idx == startIndex) startPct else 0
            if (c.downloaded) cache[c.index] = c
        }
        loading = false
    }

    /** 预取相邻章（只读缓存，不触发抓取）：切章秒开，失败静默不影响当前阅读 */
    suspend fun prefetch(idx: Int?) {
        if (idx == null || idx < 1 || cache.containsKey(idx)) return
        val r = gateway.httpText(ep.port, bookPath(bookKey, "/chapter/$idx"))
        if (r.ok) EngineData.chapter(r.body)?.let { if (it.downloaded) cache[it.index] = it }
    }

    /** 保存进度：服务端要求 idx>=1，pct 取章内百分比 */
    suspend fun saveProgress(idx: Int, pct: Int, name: String): Boolean {
        if (idx < 1) return false
        val body = JSONObject().put("idx", idx).put("pct", pct.coerceIn(0, 100))
            .put("name", name.take(100)).toString()
        val r = gateway.httpPost(ep.port, bookPath(bookKey, "/progress"), body)
        return r.ok && !r.body.contains("\"ok\": false") && !r.body.contains("\"ok\":false")
    }

    LaunchedEffect(bookKey, startIndex) { load(startIndex.coerceAtLeast(1)) }
    LaunchedEffect(index, chapter?.downloaded) {
        val c = chapter ?: return@LaunchedEffect
        if (c.downloaded && c.index == index) {
            prefetch(c.next)
            saveProgress(index, posPct, c.name)
        }
    }

    // 系统返回（手势/返回键）必须与顶栏「← 返回」一样**先保存再走**：小说侧此前只有顶栏按钮
    // 保存，而"滚动静止 1.2s"的防抖写在 ChapterBody 里 —— 连续滚动会不断取消那次写入，
    // 此时用手势退出就把最后读到的位置丢了（退出再进回到很早的位置）。
    // 漫画阅读器 2026-09-17 修过同一处（MangaScreens.kt），小说侧当时没对齐。
    BackHandler {
        // 保存限时等待 1.5s：引擎线程被占满时无界等待会把退出冻住
        // （与漫画阅读器同一修复）；实时记录几乎总已落盘，不丢进度
        scope.launch {
            kotlinx.coroutines.withTimeoutOrNull(1500) {
                saveProgress(index, posPct, chapter?.name ?: "")
            }
            onBack()
        }
    }
    // 切后台/锁屏也要保存：Android 可能在之后随时冻结进程，那时协程不会再跑
    val lifecycleOwner = androidx.lifecycle.compose.LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val obs = androidx.lifecycle.LifecycleEventObserver { _, ev ->
            if (ev == androidx.lifecycle.Lifecycle.Event.ON_STOP) {
                scope.launch { saveProgress(index, posPct, chapter?.name ?: "") }
            }
        }
        lifecycleOwner.lifecycle.addObserver(obs)
        onDispose { lifecycleOwner.lifecycle.removeObserver(obs) }
    }

    Scaffold(
        topBar = {
            Column {
                TopAppBar(
                    title = {
                        Text(
                            chapter?.name ?: "第 $index 章",
                            maxLines = 1, overflow = TextOverflow.Ellipsis,
                            fontSize = 16.sp,
                        )
                    },
                    navigationIcon = {
                        // 与系统返回手势同一语义：先保存进度再退出（限时 1.5s，不冻退出）
                        IconButton(onClick = { scope.launch {
                            kotlinx.coroutines.withTimeoutOrNull(1500) {
                                saveProgress(index, posPct, chapter?.name ?: "")
                            }
                            onBack()
                        } }) {
                            Icon(
                                Icons.AutoMirrored.Filled.ArrowBack,
                                contentDescription = "返回",
                                tint = WnColors.ink,
                            )
                        }
                    },
                    actions = {
                        TextButton(onClick = { showToc = true }) { Text("目录") }
                        IconButton(onClick = { showPrefs = true }) {
                            Icon(
                                Icons.Outlined.TextFields,
                                contentDescription = "阅读设置",
                                tint = WnColors.inkDim,
                            )
                        }
                    },
                    colors = TopAppBarDefaults.topAppBarColors(
                        containerColor = WnColors.surface,
                        titleContentColor = WnColors.ink,
                    ),
                )
                HorizontalDivider(color = WnColors.line)
            }
        },
        bottomBar = {
            Surface(color = WnColors.surface) {
                Column {
                    HorizontalDivider(color = WnColors.line)
                    Row(Modifier.fillMaxWidth().padding(horizontal = WnSpace.sm, vertical = WnSpace.xs),
                        verticalAlignment = Alignment.CenterVertically) {
                        TextButton(onClick = { scope.launch { load(index - 1) } }, enabled = index > 1) {
                            Text("上一章")
                        }
                        Text(
                            "$index / ${chapter?.total ?: 0}" + if (posPct > 0) " · $posPct%" else "",
                            style = MaterialTheme.typography.labelMedium,
                            color = WnColors.inkDim,
                            textAlign = TextAlign.Center,
                            modifier = Modifier.weight(1f),
                        )
                        TextButton(
                            onClick = { scope.launch { load(index + 1) } },
                            enabled = chapter?.next != null || index < (chapter?.total ?: 0),
                        ) { Text("下一章") }
                    }
                }
            }
        },
    ) { pad ->
        Box(Modifier.padding(pad).fillMaxSize().background(bg.bg)) {
            val c = chapter
            when {
                loading -> Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
                error != null -> Column(Modifier.fillMaxSize().padding(WnSpace.xl), Arrangement.Center) {
                    Text(error!!, color = MaterialTheme.colorScheme.error)
                    Spacer(Modifier.height(WnSpace.md))
                    Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                        Button(onClick = { scope.launch { load(index) } }) { Text("重试") }
                        OutlinedButton(onClick = { scope.launch { load(index + 1) } }) { Text("下一章") }
                    }
                }
                c == null || !c.downloaded -> NotDownloadedPanel(
                    index = index, crawling = crawling, message = crawlMsg, colors = bg,
                    // 服务端给出的原因（缓存丢失 vs 尚未下载）优先于通用文案
                    reason = c?.reason.orEmpty(),
                    onFetch = {
                        scope.launch {
                            crawling = true; crawlMsg = null
                            // 与桌面端同一语义：POST 会真的去抓这一章（可能失败，不假装成功）
                            val r = gateway.httpPost(ep.port, bookPath(bookKey, "/chapter/$index"))
                            val ok = r.ok && EngineData.recoveredContent(r.body) != null
                            crawling = false
                            if (ok) { cache.remove(index); load(index) }
                            else crawlMsg = "在线获取失败：HTTP ${r.code}" +
                                if (r.body.isNotBlank()) " · ${r.body.take(120)}" else ""
                        }
                    },
                    onRetry = { scope.launch { load(index) } },
                )
                else -> ChapterBody(
                    c = c, prefs = prefs, colors = bg,
                    restorePct = if (c.index == startIndex) startPct else 0,
                    onPos = { pct -> posPct = pct },
                    onDebouncedSave = { pct -> saveProgress(c.index, pct, c.name) },
                    onNext = { c.next?.let { n -> scope.launch { load(n) } } },
                )
            }

            if (showToc) {
                TocOverlay(
                    gateway = gateway, ep = ep, bookKey = bookKey,
                    current = index, onClose = { showToc = false },
                    onPick = { i -> showToc = false; scope.launch { load(i) } },
                )
            }
            if (showPrefs) {
                PrefsOverlay(
                    prefs = prefs,
                    onChange = { p -> prefs = p; ReaderPrefs.save(ctx, p) },
                    onClose = { showPrefs = false },
                )
            }
        }
    }
}

/** 正文：按段落渲染，滚动静止 1.2s 后保存一次进度（防抖，避免每滚一屏都写盘） */
@Composable
private fun ChapterBody(
    c: ChapterContent,
    prefs: ReaderPrefs,
    colors: ReaderColors,
    restorePct: Int,
    onPos: (Int) -> Unit,
    onDebouncedSave: suspend (Int) -> Boolean,
    onNext: () -> Unit,
) {
    val paras = c.paragraphs
    key(c.index) {
        val initial = if (restorePct > 0 && paras.size > 1) {
            (restorePct.toLong() * (paras.size - 1) / 100).toInt().coerceIn(0, paras.size - 1)
        } else 0
        val listState = rememberLazyListState(initialFirstVisibleItemIndex = initial)

        LaunchedEffect(c.index, paras.size) {
            snapshotFlow { listState.firstVisibleItemIndex }.collectLatest { first ->
                val pct = if (paras.size <= 1) 100 else (first * 100 / (paras.size - 1)).coerceIn(0, 100)
                onPos(pct)
                delay(1200)          // 停下 1.2s 再写，避免滚动过程中连续请求
                onDebouncedSave(pct)
            }
        }

        LazyColumn(
            state = listState,
            modifier = Modifier.fillMaxSize(),
            contentPadding = PaddingValues(horizontal = 20.dp, vertical = 16.dp),
        ) {
            item {
                Text(
                    c.name,
                    color = colors.fg,
                    fontSize = (prefs.fontSizeSp + 4).sp,
                    fontWeight = FontWeight.Bold,
                    lineHeight = ((prefs.fontSizeSp + 4) * prefs.lineHeightMul).sp,
                    modifier = Modifier.padding(bottom = 12.dp),
                )
            }
            items(paras) { p ->
                Text(
                    p,
                    color = colors.fg,
                    fontSize = prefs.fontSizeSp.sp,
                    lineHeight = (prefs.fontSizeSp * prefs.lineHeightMul).sp,
                    modifier = Modifier.padding(bottom = (prefs.fontSizeSp * 0.5f).dp),
                )
            }
            item {
                Spacer(Modifier.height(24.dp))
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
                    OutlinedButton(onClick = onNext, enabled = c.next != null) { Text("下一章") }
                }
                Spacer(Modifier.height(8.dp))
                Text(
                    "全 ${c.total} 章",
                    color = colors.dim,
                    style = MaterialTheme.typography.labelSmall,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(24.dp))
            }
        }
    }
}

/** 未下载/内容缺失：明确说明 + 手动在线获取（真实抓取，可能失败） */
@Composable
private fun NotDownloadedPanel(
    index: Int,
    crawling: Boolean,
    message: String?,
    colors: ReaderColors,
    onFetch: () -> Unit,
    onRetry: () -> Unit,
    reason: String = "",
) {
    Column(Modifier.fillMaxSize().padding(24.dp), Arrangement.Center, Alignment.CenterHorizontally) {
        Text("第 $index 章尚未下载", color = colors.fg, style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.height(8.dp))
        Text(
            "本机没有这一章的正文缓存。可以在线获取一次（需要联网，抓取失败会照实报错），" +
                "或回到书架让下载任务补齐后再读。",
            color = colors.dim,
            style = MaterialTheme.typography.bodySmall,
            textAlign = TextAlign.Center,
        )
        if (reason.isNotBlank()) {
            Spacer(Modifier.height(8.dp))
            Text(reason, color = colors.dim, style = MaterialTheme.typography.bodySmall,
                 textAlign = TextAlign.Center, modifier = Modifier.testTag("reader_missing_reason"))
        }
        Spacer(Modifier.height(16.dp))
        if (crawling) {
            CircularProgressIndicator()
            Spacer(Modifier.height(8.dp))
            Text("正在抓取本章…", color = colors.dim, style = MaterialTheme.typography.bodySmall)
        } else {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(onClick = onFetch) { Text("在线获取本章") }
                OutlinedButton(onClick = onRetry) { Text("重新检查") }
            }
        }
        if (message != null) {
            Spacer(Modifier.height(12.dp))
            Text(message, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall,
                 textAlign = TextAlign.Center)
        }
    }
}

/** 目录：全屏覆盖，标注已读/已下载/失败，点击切章；外壳与全局暖墨一致（bg + surface 头栏） */
@Composable
private fun TocOverlay(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    bookKey: String,
    current: Int,
    onClose: () -> Unit,
    onPick: (Int) -> Unit,
) {
    var chapters by remember { mutableStateOf<List<ChapterItem>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(bookKey) {
        val r = gateway.httpText(ep.port, bookPath(bookKey))
        val d = if (r.ok) EngineData.bookDetail(r.body) else null
        if (d == null) error = "目录加载失败：HTTP ${r.code}" else chapters = d.chapters
    }
    Surface(Modifier.fillMaxSize(), color = WnColors.bg) {
        Column(Modifier.fillMaxSize()) {
            Surface(color = WnColors.surface) {
                Row(Modifier.fillMaxWidth().padding(WnSpace.sm),
                    verticalAlignment = Alignment.CenterVertically) {
                    TextButton(onClick = onClose) { Text("关闭") }
                    Text("目录（${chapters.size} 章）", color = WnColors.ink,
                         style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                }
            }
            HorizontalDivider(color = WnColors.line)
            when {
                error != null -> Box(Modifier.fillMaxSize(), Alignment.Center) {
                    Text(error!!, color = MaterialTheme.colorScheme.error)
                }
                chapters.isEmpty() -> Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
                else -> LazyColumn(Modifier.fillMaxSize().testTag("toc_list")) {
                    items(chapters, key = { it.index }) { c ->
                        Row(
                            Modifier.fillMaxWidth().clickable { onPick(c.index) }
                                .padding(horizontal = WnSpace.lg, vertical = WnSpace.md),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Text(
                                c.label,
                                color = if (c.index == current) WnColors.accent else WnColors.ink,
                                style = MaterialTheme.typography.bodyMedium,
                                maxLines = 1, overflow = TextOverflow.Ellipsis,
                                modifier = Modifier.weight(1f),
                            )
                            if (c.failed) Text("失败", color = WnColors.danger,
                                style = MaterialTheme.typography.labelSmall)
                            else if (c.downloaded) Text("已下载", color = WnColors.inkDim,
                                style = MaterialTheme.typography.labelSmall)
                        }
                        HorizontalDivider(color = WnColors.line)
                    }
                }
            }
        }
    }
}

/** 阅读设置：字号、行距、主题；立即生效并落盘；外壳与全局暖墨一致，Slider 走 accent */
@Composable
private fun PrefsOverlay(
    prefs: ReaderPrefs,
    onChange: (ReaderPrefs) -> Unit,
    onClose: () -> Unit,
) {
    Surface(Modifier.fillMaxSize(), color = WnColors.bg) {
        Column(Modifier.fillMaxSize()) {
            Surface(color = WnColors.surface) {
                Row(Modifier.fillMaxWidth().padding(WnSpace.sm),
                    verticalAlignment = Alignment.CenterVertically) {
                    TextButton(onClick = onClose) { Text("关闭") }
                    Text("阅读设置", color = WnColors.ink, style = MaterialTheme.typography.titleMedium)
                }
            }
            HorizontalDivider(color = WnColors.line)
            Column(Modifier.fillMaxSize().padding(WnSpace.lg)) {
                Text("字号 ${prefs.fontSizeSp}", color = WnColors.ink,
                     style = MaterialTheme.typography.bodyMedium)
                Slider(
                    value = prefs.fontSizeSp.toFloat(),
                    onValueChange = { onChange(prefs.copy(fontSizeSp = it.toInt())) },
                    valueRange = ReaderPrefs.MIN_FONT.toFloat()..ReaderPrefs.MAX_FONT.toFloat(),
                    steps = ReaderPrefs.MAX_FONT - ReaderPrefs.MIN_FONT - 1,
                )
                Spacer(Modifier.height(WnSpace.sm))
                Text("行距 " + String.format(java.util.Locale.US, "%.1f", prefs.lineHeightMul),
                     color = WnColors.ink, style = MaterialTheme.typography.bodyMedium)
                Slider(
                    value = prefs.lineHeightMul,
                    onValueChange = { onChange(prefs.copy(lineHeightMul = (it * 10).toInt() / 10f)) },
                    valueRange = ReaderPrefs.MIN_LINE..ReaderPrefs.MAX_LINE,
                )
                Spacer(Modifier.height(WnSpace.md))
                Text("主题", color = WnColors.ink, style = MaterialTheme.typography.bodyMedium)
                Spacer(Modifier.height(WnSpace.xs))
                Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                    ReaderTheme.entries.forEach { t ->
                        val selected = prefs.theme == t
                        if (selected) {
                            Button(onClick = { onChange(prefs.copy(theme = t)) },
                                shape = WnPillShape) { Text(t.label) }
                        } else {
                            OutlinedButton(onClick = { onChange(prefs.copy(theme = t)) },
                                shape = WnPillShape) { Text(t.label) }
                        }
                    }
                }
                Spacer(Modifier.height(WnSpace.xl))
                Text(
                    "设置保存在本机，引擎重启或失败都不会丢；正文来自本机缓存的章节内容。",
                    color = WnColors.inkDim, style = MaterialTheme.typography.bodySmall,
                )
            }
        }
    }
}
