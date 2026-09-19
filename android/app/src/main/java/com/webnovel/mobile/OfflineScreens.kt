@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)

package com.webnovel.mobile

import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.rememberTransformableState
import androidx.compose.foundation.gestures.transformable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
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
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.ImageLoader
import coil.compose.SubcomposeAsyncImage
import coil.request.ImageRequest

/**
 * 离线模式界面（方向基线 §5.4 / §6.6）：**引擎完全不可用时**也能看书架与已下载内容。
 *
 * 过去只有引擎 ready 才显示主框架，错误页却写着"已下载内容仍可离线阅读"——
 * 用户根本进不去书架，这就是"入口承诺不一致"。现在：
 *   - 主框架始终渲染，引擎异常时顶部给一条诚实的横幅（阶段 + 重试 + 诊断）；
 *   - 书架在引擎不可用时显示**本地只读索引**（已下载的小说与漫画）；
 *   - 小说正文直接读缓存文件、漫画直接读本地图片，不需要引擎与网络。
 *
 * 能力边界照实说：离线只有"已下载"的内容；未下载的章节在这里点不开，
 * 会明确提示需要引擎（不假装可用、不静默失败）。
 */
internal data class OfflineShelfData(
    val novels: List<OfflineStore.OfflineNovel>,
    val manga: List<OfflineStore.OfflineManga>,
) {
    val isEmpty: Boolean get() = novels.isEmpty() && manga.isEmpty()
    val total: Int get() = novels.size + manga.size
}

@Composable
internal fun rememberOfflineShelf(): OfflineShelfData {
    val ctx = LocalContext.current
    // 每次进入界面重新扫描一次（体积很小：只 stat 目录与计数），
    // 但列表本身不随引擎状态变化而重建，避免闪烁
    return remember {
        OfflineShelfData(OfflineStore.novels(ctx), OfflineStore.manga(ctx))
    }
}

/** 引擎不可用横幅：说清"现在能用什么"，并给出两个真正可达的动作 */
@Composable
internal fun EngineBanner(stage: String, detail: String, onRetry: () -> Unit,
                          onDiag: () -> Unit) {
    WnHairlineCard(modifier = Modifier.padding(horizontal = WnSpace.md, vertical = 6.dp)
        .testTag("engine_banner")) {
        Column(Modifier.padding(WnSpace.md)) {
            Text("本机引擎未就绪", style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Medium, color = MaterialTheme.colorScheme.error)
            Text("阶段：$stage", style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline)
            if (detail.isNotBlank()) {
                Text(detail, style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline, maxLines = 2,
                    overflow = TextOverflow.Ellipsis)
            }
            Spacer(Modifier.height(WnSpace.xs))
            Text("离线可用：书架与「已下载」的小说正文、漫画图片；搜索/详情/下载需要引擎。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline)
            Spacer(Modifier.height(6.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                Button(onClick = onRetry, modifier = Modifier.testTag("engine_banner_retry")) {
                    Text("重试")
                }
                OutlinedButton(onClick = onDiag,
                    modifier = Modifier.testTag("engine_banner_diag")) { Text("诊断信息") }
            }
        }
    }
}

/** 离线书架：只列**已下载**的内容，并说明这一限制 */
@Composable
internal fun OfflineShelfContent(
    data: OfflineShelfData,
    onOpenNovel: (String, Int) -> Unit,
    onOpenManga: (String, String) -> Unit,
) {
    LazyColumn(Modifier.fillMaxSize().testTag("offline_shelf"),
        contentPadding = PaddingValues(WnSpace.md),
        verticalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
        item {
            Text("离线书架（引擎未就绪）",
                style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(2.dp))
            Text(
                "下面只列出**本机已下载**的内容（小说正文缓存、漫画图片）。" +
                    "未下载的章节需要引擎与网络，这里点不开。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline)
            Spacer(Modifier.height(WnSpace.xs))
            Text(data.novels.size.toString() + " 本小说 · " + data.manga.size + " 部漫画",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline)
        }
        if (data.isEmpty) {
            item {
                Text("本地还没有已下载的内容。等引擎恢复后可以在书架/详情页下载，之后即使引擎不可用也能在这里读。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline)
            }
        }
        items(data.novels, key = { "n_" + it.key }) { n ->
            WnHairlineCard(onClick = { onOpenNovel(n.key, n.startIndex) }) {
                Column(Modifier.padding(WnSpace.md)) {
                    Text(n.name, style = MaterialTheme.typography.bodyMedium,
                        fontWeight = FontWeight.Medium, maxLines = 1,
                        overflow = TextOverflow.Ellipsis)
                    Text(n.label, style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline)
                }
            }
        }
        items(data.manga, key = { "m_" + it.source + it.comicId }) { m ->
            WnHairlineCard(onClick = { onOpenManga(m.source, m.comicId) }) {
                Column(Modifier.padding(WnSpace.md)) {
                    Text(m.title, style = MaterialTheme.typography.bodyMedium,
                        fontWeight = FontWeight.Medium, maxLines = 2,
                        overflow = TextOverflow.Ellipsis)
                    Text(m.label + " · " + m.source, style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline)
                }
            }
        }
    }
}

/** 离线小说阅读器：正文来自本地缓存文件（不经过引擎） */
@Composable
internal fun OfflineNovelReaderScreen(
    novelKey: String,
    startIndex: Int,
    onBack: () -> Unit,
) {
    val ctx = LocalContext.current
    val downloaded = remember(novelKey) { OfflineStore.novelDownloadedIndices(ctx, novelKey) }
    val total = remember(novelKey) { OfflineStore.novelChapterCount(ctx, novelKey) }
    var index by rememberSaveable(novelKey) {
        mutableIntStateOf(if (downloaded.contains(startIndex)) startIndex
                          else (downloaded.firstOrNull() ?: startIndex))
    }
    var showToc by rememberSaveable { mutableStateOf(false) }
    val chapter = remember(index) { OfflineStore.readNovelChapter(ctx, novelKey, index) }

    Scaffold(topBar = {
        TopAppBar(
            title = {
                Text(chapter?.first ?: "第 $index 章",
                    maxLines = 1, overflow = TextOverflow.Ellipsis)
            },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                }
            },
            actions = {
                TextButton(onClick = { showToc = true },
                    modifier = Modifier.testTag("offline_toc")) { Text("目录") }
            },
        )
    }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize()) {
            Text("离线阅读（引擎未就绪）：正文来自本机缓存，$index / $total",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
                modifier = Modifier.padding(horizontal = WnSpace.md, vertical = WnSpace.xs))
            if (chapter == null) {
                Column(Modifier.fillMaxSize().padding(WnSpace.xl), Arrangement.Center) {
                    Text("这一章没有下载到本机", style = MaterialTheme.typography.titleSmall)
                    Spacer(Modifier.height(6.dp))
                    Text("离线模式只能读已下载的章节（已下载：${downloaded.joinToString("、")}）。" +
                        "等引擎恢复后可以补下载。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.outline)
                }
            } else {
                LazyColumn(Modifier.fillMaxSize().testTag("offline_novel_body"),
                    contentPadding = PaddingValues(WnSpace.lg)) {
                    items(chapter.second.split("\n").filter { it.isNotBlank() }) { para ->
                        Text(para, style = MaterialTheme.typography.bodyMedium,
                            modifier = Modifier.padding(bottom = 10.dp))
                    }
                }
            }
            Row(Modifier.fillMaxWidth().padding(WnSpace.md),
                horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                val prev = downloaded.lastOrNull { it < index }
                val next = downloaded.firstOrNull { it > index }
                OutlinedButton(onClick = { prev?.let { index = it } }, enabled = prev != null,
                    modifier = Modifier.weight(1f)) { Text("上一章") }
                OutlinedButton(onClick = { next?.let { index = it } }, enabled = next != null,
                    modifier = Modifier.weight(1f)) { Text("下一章") }
            }
        }
    }
    if (showToc) {
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { showToc = false },
            title = { Text("已下载章节（$total 章中 ${downloaded.size} 章）") },
            text = {
                LazyColumn(Modifier.height(320.dp).testTag("offline_toc_list")) {
                    items(downloaded) { i ->
                        TextButton(onClick = { index = i; showToc = false }) {
                            Text("第 $i 章", maxLines = 1)
                        }
                    }
                }
            },
            confirmButton = { TextButton(onClick = { showToc = false }) { Text("关闭") } },
        )
    }
}

/** 离线漫画阅读器：图片直接来自本地文件（file:// 由 Coil 读取） */
@Composable
internal fun OfflineMangaReaderScreen(
    source: String,
    comicId: String,
    loader: ImageLoader,
    onBack: () -> Unit,
) {
    val ctx = LocalContext.current
    val chapters = remember(source, comicId) {
        OfflineStore.mangaChapters(ctx, source, comicId)
    }
    var chapterIdx by rememberSaveable(source, comicId) { mutableIntStateOf(0) }
    val images = chapters.getOrNull(chapterIdx)?.second ?: emptyList()
    val listState = rememberLazyListState()
    // 缩放（与在线阅读器同一交互）：双指 1x–5x + 双击放大/复位；换话复位
    var zoom by remember { mutableStateOf(1f) }
    var panOffset by remember { mutableStateOf(Offset.Zero) }
    LaunchedEffect(chapterIdx) { zoom = 1f; panOffset = Offset.Zero }
    val transformState = rememberTransformableState { zoomChange, panChange, _ ->
        val next = (zoom * zoomChange).coerceIn(1f, 5f)
        zoom = next
        panOffset = if (next > 1f) panOffset + panChange else Offset.Zero
    }

    Scaffold(topBar = {
        TopAppBar(
            title = {
                Text("离线阅读 · 第 ${chapterIdx + 1}/${chapters.size} 话",
                    maxLines = 1, overflow = TextOverflow.Ellipsis)
            },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                }
            },
        )
    }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize()) {
            Text("离线阅读（引擎未就绪）：${images.size} 张图片来自本机",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
                modifier = Modifier.padding(horizontal = WnSpace.md, vertical = WnSpace.xs))
            Box(
                Modifier.fillMaxSize().weight(1f)
                    .graphicsLayer {
                        scaleX = zoom; scaleY = zoom
                        translationX = panOffset.x; translationY = panOffset.y
                    }
                    // 只有放大状态才接管手势，否则让 LazyColumn 正常滚动
                    .transformable(state = transformState, enabled = zoom > 1f)
                    .pointerInput(chapterIdx) {
                        detectTapGestures(onDoubleTap = {
                            if (zoom > 1f) { zoom = 1f; panOffset = Offset.Zero } else { zoom = 2f }
                        })
                    },
            ) {
            LazyColumn(Modifier.fillMaxSize().testTag("offline_manga_pages"),
                state = listState,
                verticalArrangement = Arrangement.spacedBy(WnSpace.xs)) {
                items(images) { f ->
                    // 与在线阅读器同一排版：按图片原始比例，不写死宽高比
                    // （此前固定 0.7f，长条漫在离线模式被裁掉一截）
                    SubcomposeAsyncImage(
                        model = ImageRequest.Builder(ctx).data(f).build(),
                        imageLoader = loader,
                        contentDescription = f.name,
                        contentScale = ContentScale.FillWidth,
                        modifier = Modifier.fillMaxWidth().background(WnColors.bg),
                        loading = {
                            Box(Modifier.fillMaxWidth().aspectRatio(0.7f),
                                Alignment.Center) {
                                androidx.compose.material3.CircularProgressIndicator(
                                    strokeWidth = 2.dp)
                            }
                        },
                        error = {
                            Box(Modifier.fillMaxWidth().aspectRatio(0.7f),
                                Alignment.Center) {
                                Text("图片读取失败", color = WnColors.danger,
                                    style = MaterialTheme.typography.labelSmall)
                            }
                        },
                    )
                }
            }
            }
            Row(Modifier.fillMaxWidth().padding(WnSpace.md),
                horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                OutlinedButton(onClick = { chapterIdx-- }, enabled = chapterIdx > 0,
                    modifier = Modifier.weight(1f)) { Text("上一话") }
                OutlinedButton(onClick = { chapterIdx++ },
                    enabled = chapterIdx < chapters.size - 1,
                    modifier = Modifier.weight(1f)) { Text("下一话") }
            }
        }
    }
}

/**
 * 离线设置：只保留**不依赖引擎**的部分。
 *
 * 引擎起不来时，用户仍然需要能改阅读排版、能看到"装的是哪个包"、
 * 能看到本地诊断——这些都不需要引擎。其余设置（书源管理、备份恢复、
 * 任务恢复）如实标注需要引擎，而不是留一个点了报错的入口。
 */
@Composable
internal fun OfflineSettingsScreen(onOpenReaderPrefs: () -> Unit) {
    var showDiag by remember { mutableStateOf(false) }
    LazyColumn(Modifier.fillMaxSize().testTag("offline_settings"),
        contentPadding = PaddingValues(WnSpace.md),
        verticalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
        item {
            Text("设置（离线可用部分）", style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(WnSpace.xs))
            Text("阅读排版、产物信息与本地诊断不依赖引擎；书源管理、备份恢复、" +
                "任务恢复、网络代理需要引擎就绪。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline)
        }
        item {
            WnHairlineCard(modifier = Modifier.testTag("offline_reader_prefs"),
                onClick = { onOpenReaderPrefs() }) {
                Column(Modifier.padding(WnSpace.md)) {
                    Text("阅读设置", style = MaterialTheme.typography.bodyMedium,
                        fontWeight = FontWeight.Medium)
                    Text("字号 / 行距 / 主题；漫画纵向连续或横向翻页",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline)
                }
            }
        }
        item {
            WnHairlineCard(modifier = Modifier.testTag("offline_build_info"),
                onClick = { showDiag = !showDiag }) {
                Column(Modifier.padding(WnSpace.md)) {
                    Text("产物信息", style = MaterialTheme.typography.bodyMedium,
                        fontWeight = FontWeight.Medium)
                    Text(BuildInfo.summary, style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline)
                }
            }
        }
        if (showDiag) {
            item {
                Text(
                    "本机 android 信息：\n" +
                        "系统 Android ${android.os.Build.VERSION.RELEASE}" +
                        "（API ${android.os.Build.VERSION.SDK_INT}）\n" +
                        "ABI ${android.os.Build.SUPPORTED_ABIS.joinToString(",")}\n" +
                        "设备 ${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL}\n" +
                        "引擎：未就绪（离线模式）",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                    modifier = Modifier.testTag("offline_diag_text"))
            }
        }
    }
}
