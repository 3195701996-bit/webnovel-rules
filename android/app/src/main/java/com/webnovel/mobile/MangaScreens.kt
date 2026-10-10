@file:OptIn(ExperimentalMaterial3Api::class)

package com.webnovel.mobile

import android.net.Uri
import android.view.KeyEvent as AndroidKeyEvent
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.rememberTransformableState
import androidx.compose.foundation.gestures.transformable
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.focusable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.activity.compose.BackHandler
import androidx.compose.runtime.DisposableEffect
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LinearProgressIndicator
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
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.compose.SubcomposeAsyncImage
import coil.request.ImageRequest
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch
import kotlin.math.roundToInt
import org.json.JSONObject

/** 漫画相关路径：source / comic_id / chapter_id 都可能是非 ASCII，必须转义 */
private fun mangaPath(source: String, comicId: String, suffix: String = ""): String =
    "/api/manga/${Uri.encode(source)}/${Uri.encode(comicId)}$suffix"

private fun downloadStartMessage(response: HttpText, success: String): String {
    if (!response.ok) return "创建失败：HTTP ${response.code} ${response.body.take(80)}"
    val favorited = runCatching {
        JSONObject(response.body).optBoolean("favorited", true)
    }.getOrDefault(true)
    return if (favorited) success
    else "$success；但自动收藏失败，请稍后手动收藏"
}

internal data class MangaFavoriteMutationResult(
    val success: Boolean,
    val favorited: Boolean,
    val message: String,
)

/** Require both a successful HTTP response and the API's explicit business success. */
internal fun mangaFavoriteMutationResult(
    response: HttpText,
    adding: Boolean,
    wasFavorited: Boolean,
): MangaFavoriteMutationResult {
    val body = runCatching { JSONObject(response.body) }.getOrNull()
    val success = response.ok && body?.optBoolean("ok", false) == true
    if (success) {
        val favorited = if (adding) body?.optBoolean("favorited", true) != false else false
        return MangaFavoriteMutationResult(
            success = true,
            favorited = favorited,
            message = if (favorited) "已加入收藏" else "已取消收藏",
        )
    }
    val detail = body?.optString("error")?.takeIf { it.isNotBlank() }
        ?: body?.optString("message")?.takeIf { it.isNotBlank() }
        ?: if (response.code > 0) "HTTP ${response.code}" else "连接失败"
    return MangaFavoriteMutationResult(
        success = false,
        favorited = wasFavorited,
        message = "收藏操作失败：$detail",
    )
}

private fun coverUrl(port: Int, source: String, comicId: String): String =
    "http://127.0.0.1:$port" + mangaPath(source, comicId, "/cover")

/** 把服务端返回的本机 API 图片路径收敛到严格本地目录，保留其它查询参数。 */
internal fun enforceLocalCatalog(url: String): String {
    val path = url.substringBefore('?')
    val params = url.substringAfter('?', "").split('&')
        .filter { it.isNotBlank() && !it.startsWith("catalog=") }
        .toMutableList()
    params += "catalog=local"
    return "$path?${params.joinToString("&")}"
}

internal fun appendImageRetry(url: String, tick: Int): String =
    if (tick <= 0) url else url + (if ('?' in url) "&" else "?") + "r=$tick"

/**
 * 单页图片地址解析（/urls 条目 + 重试代次 → 最终加载地址）：
 * 本地直出/懒下载通道走本机引擎；源站 CDN 首次直连、失败后降级服务器代理。
 * 阅读器与详情页预载共用，保证缓存键一致（秒开命中）。
 */
internal fun resolveMangaPageUrl(port: Int, source: String, comicId: String,
                                 chapterId: String, entry: MangaPageEntry?,
                                 index: Int, tick: Int,
                                 localCatalog: Boolean = false): String = when {
    entry == null ->
        "http://127.0.0.1:$port" +
            mangaPath(source, comicId, "/chapter/${Uri.encode(chapterId)}/img/$index") +
            (if (localCatalog) "?catalog=local" else "") +
            (if (tick > 0) (if (localCatalog) "&" else "?") + "r=$tick" else "")
    entry.url.startsWith("/api/") -> {
        val apiUrl = if (localCatalog) enforceLocalCatalog(entry.url) else entry.url
        appendImageRetry("http://127.0.0.1:$port$apiUrl", tick)
    }
    // 本地目录是严格离线入口：即使旧服务端/异常缓存返了 CDN，也只向本机
    // catalog=local 图片端点请求；缺页时由引擎返回明确 404，而非 Coil 直连公网。
    localCatalog -> appendImageRetry(
        "http://127.0.0.1:$port" + mangaPath(source, comicId,
            "/chapter/${Uri.encode(chapterId)}/img/$index?catalog=local"), tick)
    tick == 0 -> entry.url
    else ->
        "http://127.0.0.1:$port" + mangaPath(source, comicId,
            "/chapter/${Uri.encode(chapterId)}/proxy?u=${Uri.encode(entry.url)}")
}

/**
 * 阅读秒开缓存：详情页预载的「详情」与「章节 /urls」（TTL 60s、上限 24 条）。
 * 用户从详情页点「开始/继续阅读」时，阅读器免去两次串行回环往返（详情 + urls），
 * 配合首屏页图片预载，实现章节秒开。
 */
internal object MangaReadCache {
    private const val TTL_MS = 60_000L
    private const val MAX_ENTRIES = 24
    private val lock = Any()
    private val details = LinkedHashMap<String, Pair<Long, MangaDetail>>()
    private val urls = LinkedHashMap<String, Pair<Long, MangaChapterPages>>()

    private fun <T> get(map: LinkedHashMap<String, Pair<Long, T>>, key: String): T? =
        synchronized(lock) {
            val e = map[key] ?: return null
            if (System.currentTimeMillis() - e.first > TTL_MS) {
                map.remove(key); null
            } else e.second
        }

    private fun <T> put(map: LinkedHashMap<String, Pair<Long, T>>, key: String, v: T) {
        synchronized(lock) {
            map[key] = System.currentTimeMillis() to v
            while (map.size > MAX_ENTRIES) {
                map.remove(map.entries.first().key)
            }
        }
    }

    private fun dk(source: String, comicId: String, localCatalog: Boolean) =
        "$source|$comicId|local=$localCatalog"
    private fun uk(source: String, comicId: String, chapterId: String,
                   localCatalog: Boolean) = "$source|$comicId|$chapterId|local=$localCatalog"

    fun getDetail(source: String, comicId: String,
                  localCatalog: Boolean = false): MangaDetail? =
        get(details, dk(source, comicId, localCatalog))
    fun putDetail(source: String, comicId: String, d: MangaDetail,
                  localCatalog: Boolean = false) =
        put(details, dk(source, comicId, localCatalog), d)
    fun getUrls(source: String, comicId: String, chapterId: String,
                localCatalog: Boolean = false): MangaChapterPages? =
        get(urls, uk(source, comicId, chapterId, localCatalog))
    fun putUrls(source: String, comicId: String, chapterId: String, p: MangaChapterPages,
                localCatalog: Boolean = false) =
        put(urls, uk(source, comicId, chapterId, localCatalog), p)

    /** Download completion/deletion changes local-vs-network image routing. */
    fun invalidate(source: String, comicId: String) = synchronized(lock) {
        details.remove(dk(source, comicId, false))
        details.remove(dk(source, comicId, true))
        val prefix = "$source|$comicId|"
        urls.keys.removeAll { it.startsWith(prefix) }
    }
}

/**
 * 源站封面地址 → 本机封面代理地址。
 *
 * 搜索/浏览出来的漫画还没进书库，per-comic 封面接口只会查书库记录（404），
 * 而禁漫图床校验 Referer（直连 403）——所以必须由服务端带该源的防盗链头去取。
 */
internal fun remoteCoverUrl(ep: EngineEndpoint, source: String, url: String): String =
    if (url.isBlank()) "" else
        ep.imageOrigin + "/api/manga/cover?source=" + android.net.Uri.encode(source) +
            "&url=" + android.net.Uri.encode(url)

// ── 漫画详情页（原生）────────────────────────────────────────

@Composable
fun MangaDetailScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    source: String,
    comicId: String,
    loader: coil.ImageLoader,
    onBack: () -> Unit,
    /** 打开阅读器：(章节下标, 页码, 该落点是否精确, 说明文案, 章节id, 章名) */
    onRead: (Int, Int, Boolean, String, String, String) -> Unit,
    /** 点击作者/标签：跳到本源的搜索（关键词即作者名或标签名） */
    onSearchTag: (String) -> Unit,
    /** 书架入口只显示本地实盘目录；搜索/历史/收藏使用完整源站目录。 */
    localCatalog: Boolean = false,
) {
    var detail by remember { mutableStateOf<MangaDetail?>(null) }
    var readIdx by remember { mutableIntStateOf(-1) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var actionMsg by remember { mutableStateOf<String?>(null) }
    // 选择下载：只允许勾选**未下载**的话（已下载的不需要再下）
    var selected by remember { mutableStateOf<Set<String>>(emptySet()) }
    // 删除管理：开启后已下载的话/整卷可勾选删除（与下载勾选互斥）
    var manageMode by remember { mutableStateOf(false) }
    var deleteSel by remember { mutableStateOf<Set<String>>(emptySet()) }
    var confirmDelete by remember { mutableStateOf(false) }
    var deleting by remember { mutableStateOf(false) }
    var checking by remember { mutableStateOf(false) }
    var update by remember { mutableStateOf<MangaUpdateCheck?>(null) }
    var favoritedState by remember(source, comicId) { mutableStateOf<Boolean?>(null) }
    var favoriteBusy by remember(source, comicId) { mutableStateOf(false) }
    var exporting by remember { mutableStateOf(false) }
    var confirmRemove by remember { mutableStateOf(false) }
    val ctx = androidx.compose.ui.platform.LocalContext.current
    val scope = rememberCoroutineScope()

    // 导出已下载话为 zip（服务端按本地图片打包并流式返回）
    val zipLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.CreateDocument("application/zip")
    ) { uri ->
        if (uri == null) {
            actionMsg = "已取消导出"
            return@rememberLauncherForActivityResult
        }
        scope.launch {
            exporting = true; actionMsg = "正在打包导出…"
            actionMsg = try {
                val r = Export.streamToUri(ctx, ep.port, ep.token,
                    mangaPath(source, comicId, "/zip"), uri)
                "已导出 ${Export.human(r.bytes)}"
            } catch (t: kotlinx.coroutines.CancellationException) {
                throw t
            } catch (t: Throwable) {
                "导出失败：${t.message ?: t.javaClass.simpleName}"
            }
            exporting = false
        }
    }

    /** 秒开预载：续读话的 /urls 与首屏页提前拉进缓存（点开始阅读零等待） */
    fun prestage(d: MangaDetail, idx: Int) {
        MangaReadCache.putDetail(source, comicId, d, localCatalog)
        val ch = d.readingChapters.getOrNull(if (idx >= 0) idx else 0) ?: return
        if (MangaReadCache.getUrls(source, comicId, ch.id, localCatalog) != null) return
        scope.launch {
            val r = gateway.httpText(ep.port,
                mangaPath(source, comicId, "/chapter/${Uri.encode(ch.id)}/urls") +
                    if (localCatalog) "?catalog=local" else "")
            val p = if (r.ok) EngineData.mangaChapterUrls(r.body) else null
            if (p != null && p.count > 0) {
                MangaReadCache.putUrls(source, comicId, ch.id, p, localCatalog)
                // 首屏 3 页预载（缓存键与阅读器 pageRequest 一致 → 打开即命中）
                for (i in 0..minOf(2, p.count - 1)) {
                    val u = resolveMangaPageUrl(ep.imagePort, d.source, d.comicId, ch.id,
                        p.entries.getOrNull(i), i, 0, localCatalog)
                    if (u.isNotBlank()) {
                        loader.enqueue(coil.request.ImageRequest.Builder(ctx).data(u)
                            .memoryCacheKey(u).diskCacheKey(u).build())
                    }
                }
            }
        }
    }

    fun reload() {
        scope.launch {
            loading = true; error = null
            // 详情获取失败自动重试 1 次——但**只在快速失败时**重试（间歇风控的
            // 瞬时失败大多能自愈）；首次已等满 ~30s 超时说明链路不通，再重试
            // 只会让"打不开"翻倍成"一分钟打不开"（实测教训）
            // 本地详情响应自身已携带服务端解析的 resume；不额外等完整历史接口，
            // 避免书架打开本地漫画被与图片无关的网络请求拖慢。
            val hD = if (localCatalog) null else
                async { gateway.httpText(ep.port, "/api/manga/history") }
            val favD = if (localCatalog) null else
                async { gateway.httpText(ep.port, "/api/manga/favorites") }
            val t0 = System.currentTimeMillis()
            val catalogSuffix = if (localCatalog) "?catalog=local" else ""
            var r = gateway.httpText(ep.port, mangaPath(source, comicId, catalogSuffix))
            var d = if (r.ok) EngineData.mangaDetail(r.body) else null
            if (d == null && System.currentTimeMillis() - t0 < 12_000) {
                kotlinx.coroutines.delay(1500)
                r = gateway.httpText(ep.port, mangaPath(source, comicId, catalogSuffix))
                d = if (r.ok) EngineData.mangaDetail(r.body) else null
            }
            if (d == null) {
                error = "读取漫画详情失败：HTTP ${r.code}" +
                    if (r.body.isNotBlank()) " · ${EngineData.httpErrorMessage(r.body)}" else ""
            } else {
                detail = d
                if (localCatalog) {
                    val resume = d.resume
                    readIdx = if (resume?.hasRecord == true) {
                        MangaResumeResolver.resolve(
                            d.readingChapters,
                            resume.matchedId.ifBlank { resume.recordId },
                            resume.recordLabel,
                            resume.index,
                        )
                    } else -1
                    prestage(d, readIdx)
                    val localDetail = d
                    // 收藏状态是非关键元数据，后台加载，不能挡住本地目录与阅读入口。
                    scope.launch {
                        val fav = gateway.httpText(ep.port, "/api/manga/favorites")
                        val parsed = if (fav.ok)
                            EngineData.mangaFavoritesOrNull(fav.body) else null
                        if (parsed != null) {
                            favoritedState = parsed.any {
                                sameMangaIdentitySource(it.identitySource,
                                    localDetail.identitySource.ifBlank { source }) &&
                                    it.comicId == localDetail.comicId.ifBlank { comicId }
                            }
                        }
                    }
                    loading = false
                    return@launch
                }
                val fav = favD!!.await()
                val parsedFavorites = if (fav.ok)
                    EngineData.mangaFavoritesOrNull(fav.body) else null
                if (parsedFavorites != null) {
                    favoritedState = parsedFavorites.any {
                        sameMangaIdentitySource(it.identitySource, d.identitySource.ifBlank { source }) &&
                            it.comicId == d.comicId.ifBlank { comicId }
                    }
                } else {
                    actionMsg = "收藏状态读取失败：" + if (fav.ok)
                        "响应格式异常，可重试" else EngineData.httpErrorMessage(fav.body,
                            fallback = "HTTP ${fav.code}")
                }
                // 阅读位置取自历史（与网页端同一份 _history.json）。
                // 用**本页请求用的** source/comicId 匹配，而不是响应里回显的那两个字段：
                // 服务端历史详情有四条返回路径，历史上 quick 路径只给 "id" 不给 "comic_id"，
                // 回显字段曾为空串 → 匹配失败 → 续读落点退回第 1 话
                // （用户 2026-09-16 反馈"明明记了进度却从第一话开始"）。
                // 服务端已补齐字段，这里再做一层"不依赖回显"的防御。
                val h = hD!!.await()
                if (!h.ok) {
                    readIdx = -1
                    detail = d.copy(resume = null)
                    actionMsg = "阅读历史暂不可读，已暂停自动续读定位：" +
                        EngineData.httpErrorMessage(h.body, fallback = "HTTP ${h.code}")
                } else {
                    readIdx = EngineData.mangaHistory(h.body).firstOrNull {
                        sameMangaIdentitySource(it.identitySource, d.identitySource.ifBlank { source }) &&
                            it.comicId == (d.comicId.ifBlank { comicId })
                    }?.idx ?: -1
                }
                // 秒开预载：本地书库和在线详情都预载续读话 /urls + 首屏页。
                // 本地书架之前跳过了预载，阅读器只能串行取目录与图片清单，导致
                // 已下载漫画打开速度明显慢于在线详情。
                prestage(d, readIdx)
            }
            loading = false
        }
    }
    // 目录模式属于详情请求身份的一部分：同作品从完整目录切到本地书架目录时，
    // 必须取消旧请求并重载，不能只靠按模式分开的缓存而继续显示旧目录。
    LaunchedEffect(source, comicId, localCatalog) { reload() }

    // ── 下载任务状态（轮询）：章节「下载中/排队中」标注 + 实时进度 + 暂停/继续 ──
    var dl by remember { mutableStateOf<EngineData.MangaDlStatus?>(null) }
    var dlBusy by remember { mutableStateOf(false) }
    var pollTick by remember { mutableIntStateOf(0) }

    suspend fun fetchDl(): EngineData.MangaDlStatus {
        val r = gateway.httpText(ep.port,
            "/api/manga/download/status?source=${Uri.encode(source)}" +
                "&cid=${Uri.encode(comicId)}")
        return EngineData.mangaDlStatus(if (r.ok) r.body else "")
    }

    LaunchedEffect(source, comicId, pollTick) {
        // 进入页面先取一次；任务进行中每 2s 轮询；转为非进行中（完成/暂停/失败）
        // 时刷新详情（已下载集合是磁盘实扫，需重新取）后停止
        while (true) {
            val wasActive = dl?.active == true
            val st = fetchDl()
            dl = st
            if (!st.active) {
                if (wasActive || st.status == "done") {
                    MangaReadCache.invalidate(source, comicId)
                    OfflineStore.invalidateMangaIndex(OfflineStore.runtimeDir(ctx), source, comicId)
                    reload()
                }
                break
            }
            kotlinx.coroutines.delay(2000)
        }
    }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text(detail?.title ?: "漫画详情", maxLines = 1, overflow = TextOverflow.Ellipsis) },
            colors = androidx.compose.material3.TopAppBarDefaults.topAppBarColors(
                containerColor = WnColors.bg,
                titleContentColor = WnColors.ink,
                navigationIconContentColor = WnColors.ink,
            ),
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                }
            },
        )
    }) { pad ->
        val d = detail
        Box(Modifier.padding(pad).fillMaxSize()) {
            when {
                loading -> Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
                d == null -> Column(Modifier.fillMaxSize().padding(WnSpace.xl), Arrangement.Center) {
                    Text(error ?: "漫画不存在", color = MaterialTheme.colorScheme.error)
                    Spacer(Modifier.height(WnSpace.md))
                    Button(onClick = { reload() }) { Text("重试") }
                }
                else -> LazyColumn(Modifier.fillMaxSize().testTag("manga_detail_list"),
                    contentPadding = PaddingValues(bottom = 24.dp)) {
                    item {
                        Row(Modifier.fillMaxWidth().padding(horizontal = WnSpace.md,
                            vertical = WnSpace.lg).clip(WnCardShape)
                            .background(WnColors.surface).padding(WnSpace.md)) {
                            SubcomposeAsyncImage(
                                model = ImageRequest.Builder(androidx.compose.ui.platform.LocalContext.current)
                                    .data(coverUrl(ep.imagePort, d.source, d.comicId))
                                    .crossfade(true)
                                    .build(),
                                imageLoader = loader,
                                contentDescription = d.title,
                                contentScale = ContentScale.Crop,
                                modifier = Modifier.width(110.dp).aspectRatio(0.72f)
                                    .background(MaterialTheme.colorScheme.surfaceVariant),
                                loading = { Box(Modifier.fillMaxSize()) },
                                error = {
                                    // 非书库漫画（搜索/浏览进来的）per-comic 封面可能没有：
                                    // 回落按源站地址取图的代理，再失败才显示"无封面"
                                    val fb = remoteCoverUrl(ep, d.source, d.cover)
                                    if (fb.isNotBlank()) {
                                        SubcomposeAsyncImage(
                                            model = ImageRequest.Builder(
                                                androidx.compose.ui.platform.LocalContext.current)
                                                .data(fb).crossfade(true).build(),
                                            imageLoader = loader,
                                            contentDescription = d.title,
                                            contentScale = ContentScale.Crop,
                                            modifier = Modifier.fillMaxSize(),
                                            loading = { Box(Modifier.fillMaxSize()) },
                                            error = { Box(Modifier.fillMaxSize(), Alignment.Center) {
                                                Text("暂无封面",
                                                     style = MaterialTheme.typography.labelSmall)
                                            } },
                                        )
                                    } else {
                                        Box(Modifier.fillMaxSize(), Alignment.Center) {
                                            Text("暂无封面",
                                                 style = MaterialTheme.typography.labelSmall)
                                        }
                                    }
                                },
                            )
                            Spacer(Modifier.width(WnSpace.md))
                            Column(Modifier.weight(1f)) {
                                Text(d.title, style = MaterialTheme.typography.titleLarge,
                                     fontWeight = FontWeight.Bold, maxLines = 3,
                                     overflow = TextOverflow.Ellipsis)
                                Spacer(Modifier.height(WnSpace.sm))
                                // 作者：点击 → 该源内搜索此作者（对齐 web/venera 标签交互）
                                Row(verticalAlignment = Alignment.CenterVertically,
                                    horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                                    WnTagChip("✎ " + d.author.ifBlank { "作者未知" },
                                        accent = true) {
                                        if (d.author.isNotBlank()) onSearchTag(d.author)
                                    }
                                    if (d.sourceName.isNotBlank()) {
                                        Text("· " + d.sourceName,
                                            style = MaterialTheme.typography.bodySmall,
                                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                                    }
                                }
                                Spacer(Modifier.height(WnSpace.xs))
                                Text(
                                    buildString {
                                        append("共 ${d.readingChapters.size} 单元")
                                        if (d.volumes.isNotEmpty()) append(" + ${d.volumes.size} 整卷")
                                        append(" · 已下载 ${d.downloadedCount}")
                                    },
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    modifier = Modifier.testTag("manga_detail_counts"),
                                )
                                if (d.tags.isNotEmpty()) {
                                    Spacer(Modifier.height(WnSpace.xs))
                                    // 标签：点击 → 该源内搜索此标签（可横向滑动）
                                    Row(
                                        Modifier.fillMaxWidth()
                                            .horizontalScroll(
                                                androidx.compose.foundation.rememberScrollState()),
                                        horizontalArrangement = Arrangement.spacedBy(WnSpace.xs),
                                    ) {
                                        d.tags.forEach { t ->
                                            WnTagChip(t) { onSearchTag(t) }
                                        }
                                    }
                                }
                            }
                        }
                        if (d.intro.isNotBlank()) {
                            Text(d.intro, style = MaterialTheme.typography.bodySmall,
                                 color = MaterialTheme.colorScheme.onSurfaceVariant,
                                 maxLines = 4, overflow = TextOverflow.Ellipsis,
                                 modifier = Modifier.padding(horizontal = WnSpace.lg))
                            Spacer(Modifier.height(WnSpace.sm))
                        }
                        val resume = d.resumeIndex(readIdx)
                        val readingChapters = d.readingChapters
                        val res = d.resume
                        Row(Modifier.fillMaxWidth().padding(horizontal = WnSpace.lg),
                            horizontalArrangement = Arrangement.End) {
                            TextButton(onClick = {
                                scope.launch {
                                    val before = favoritedState == true
                                    favoriteBusy = true
                                    actionMsg = if (before) "正在取消收藏…" else "正在加入收藏…"
                                    try {
                                        val r = if (before) gateway.httpDelete(ep.port,
                                            "/api/manga/favorites/${Uri.encode(d.source)}/${Uri.encode(d.comicId)}")
                                        else gateway.httpPost(ep.port, "/api/manga/favorites",
                                            JSONObject().put("source", d.source)
                                                .put("comic_id", d.comicId).put("title", d.title)
                                                .put("cover", d.cover).toString())
                                        val result = mangaFavoriteMutationResult(r, !before, before)
                                        favoritedState = result.favorited
                                        actionMsg = result.message
                                    } catch (t: Throwable) {
                                        favoritedState = before
                                        actionMsg = "收藏操作失败：${t.message ?: "连接失败"}"
                                    } finally {
                                        favoriteBusy = false
                                    }
                                }
                            }, enabled = favoritedState != null && !favoriteBusy) {
                                Text(when {
                                    favoriteBusy -> "处理中…"
                                    favoritedState == true -> "取消收藏"
                                    else -> "收藏"
                                })
                            }
                        }
                        // 三种状态必须分清楚（0.64.0）：
                        //   · 完全没有阅读记录 → "开始阅读"（新漫画就该这么说）
                        //   · 记录能精确定位   → "继续阅读 <话名>"
                        //   · 记录只能近似定位 → "继续阅读（<话名>）" + 一行说明
                        // 上一版把"无记录"也当成近似，新漫画显示成"继续阅读（第1话…）"，
                        // 而且让四条既有用例（都点"开始阅读"）全部超时——实测抓出。
                        val hasRecord = res != null || readIdx >= 0
                        val exact = when {
                            res != null -> res.exact
                            readIdx >= 0 -> true
                            else -> false
                        }
                        val note = res?.note.orEmpty()
                        Row(Modifier.fillMaxWidth().padding(horizontal = WnSpace.lg)) {
                            Button(
                                onClick = {
                                    // 页码也要续（记录形如 "第47话 P30"，书库那行就写着
                                    // P30）；同时把该话的**身份**传给阅读器，让它在自己
                                    // 的列表里再定位一次（两份列表可能不一致）。
                                    if (resume >= 0) {
                                        val ch = readingChapters[resume]
                                        // trusted：只有"有记录、但没能精确定位"时才是 false
                                        // （那时阅读器不写回，避免覆盖用户进度）。
                                        // 完全没有记录时必须 trusted=true —— 否则第一次阅读
                                        // 也不会保存，永远建不出记录（实测：详情页永远停在
                                        // "开始阅读"，四条用例连锁超时）。
                                        onRead(resume, res?.page ?: 1, !hasRecord || exact,
                                               note, ch.id, ch.label)
                                    }
                                },
                                enabled = resume >= 0,
                                modifier = Modifier.weight(1f),
                            ) {
                                Text(
                                    when {
                                        resume < 0 -> "暂无可读章节"
                                        !hasRecord -> "开始阅读"
                                        exact -> "继续阅读 ${readingChapters[resume].label}"
                                        else -> "继续阅读（${readingChapters[resume].label}）"
                                    },
                                    maxLines = 1, overflow = TextOverflow.Ellipsis,
                                )
                            }
                        }
                        if (note.isNotBlank() && hasRecord) {
                            Text(
                                note,
                                color = WnColors.accent,
                                style = MaterialTheme.typography.labelSmall,
                                modifier = Modifier.padding(horizontal = WnSpace.lg, vertical = 2.dp)
                                    .testTag("manga_resume_note"),
                            )
                        }
                        Spacer(Modifier.height(6.dp))
                        Row(Modifier.fillMaxWidth().padding(horizontal = WnSpace.lg),
                            horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                            OutlinedButton(
                                onClick = {
                                    zipLauncher.launch(Export.safeFileName(d.title, comicId) + ".zip")
                                },
                                enabled = !exporting && d.downloadedCount > 0,
                                modifier = Modifier.weight(1f),
                            ) { Text(if (exporting) "导出中…" else "导出 ZIP（${d.downloadedCount} 话）") }
                            OutlinedButton(
                                onClick = {
                                    // 只统计「话」——整卷需用户在下方整卷区块自行勾选，
                                    // 不参与"下载未下载话"的默认清单
                                    val missing = d.readingChapters
                                        .filterNot { d.downloaded.contains(it.id) }
                                    if (missing.isEmpty()) {
                                        actionMsg = "所有内容都已下载"
                                    } else {
                                        scope.launch {
                                            // 只提交"未下载的话"这一份清单，服务端据此增量下载
                                            val body = org.json.JSONObject()
                                                .put("title", d.title)
                                                .put("cover", d.cover)
                                                .put("chapters", org.json.JSONArray(
                                                    missing.map { it.id }))
                                                .toString()
                                            val r = gateway.httpPost(ep.port,
                                                mangaPath(d.source, d.comicId, "/download"), body)
                                            if (r.ok) pollTick++
                                            actionMsg = downloadStartMessage(
                                                r, "已创建下载任务：${missing.size} 个单元（见下载页）")
                                        }
                                    }
                                },
                                enabled = d.readingChapters.any { !d.downloaded.contains(it.id) },
                                modifier = Modifier.weight(1f),
                            ) { Text("补齐下载") }
                        }
                        Spacer(Modifier.height(6.dp))
                        Row(Modifier.fillMaxWidth().padding(horizontal = WnSpace.lg),
                            horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                            OutlinedButton(
                                onClick = {
                                    scope.launch {
                                        checking = true
                                        update = null
                                        val r = gateway.httpPost(ep.port,
                                            mangaPath(d.source, d.comicId, "/check-update"))
                                        update = if (r.ok) EngineData.mangaUpdateCheck(r.body)
                                                 else MangaUpdateCheck(
                                                     false, false, 0, "", "HTTP ${r.code}", emptyList())
                                        checking = false
                                    }
                                },
                                enabled = !checking,
                                modifier = Modifier.weight(1f).testTag("check_update"),
                            ) { Text(if (checking) "检查中…" else "检查更新") }
                            update?.let { u ->
                                if (u.ok && (u.hasUpdate || u.missingCount > 0)) {
                                    Button(
                                        onClick = {
                                            // 有新话：有明确章节 id 就下这些，否则退回"下载未下载话"
                                            val ids = u.missingIds
                                            val missing = if (ids.isNotEmpty()) ids
                                                else d.readingChapters.filterNot { d.downloaded.contains(it.id) }
                                                    .map { it.id }
                                            scope.launch {
                                                val body = org.json.JSONObject()
                                                    .put("title", d.title).put("cover", d.cover)
                                                    .put("chapters", org.json.JSONArray(missing))
                                                    .toString()
                                                val rr = gateway.httpPost(ep.port,
                                                    mangaPath(d.source, d.comicId, "/download"), body)
                                                if (rr.ok) pollTick++
                                                actionMsg = downloadStartMessage(
                                                    rr, "已创建下载任务：新话 ${missing.size} 个（见下载页）")
                                            }
                                        },
                                        modifier = Modifier.weight(1f),
                                    ) { Text("下载新话") }
                                }
                            }
                        }
                        update?.let { u ->
                            Spacer(Modifier.height(4.dp))
                            Text(
                                u.label,
                                style = MaterialTheme.typography.labelMedium,
                                color = if (u.ok) MaterialTheme.colorScheme.primary
                                        else MaterialTheme.colorScheme.error,
                                modifier = Modifier.padding(horizontal = WnSpace.lg),
                            )
                        }
                        if (selected.isNotEmpty()) {
                            Spacer(Modifier.height(6.dp))
                            Row(Modifier.fillMaxWidth().padding(horizontal = WnSpace.lg),
                                horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                                Button(
                                    onClick = {
                                        scope.launch {
                                            val ids = d.readingChapters.filter { selected.contains(it.id) }
                                                .map { it.id }
                                            val body = org.json.JSONObject()
                                                .put("title", d.title)
                                                .put("cover", d.cover)
                                                .put("chapters", org.json.JSONArray(ids))
                                                .toString()
                                            val r = gateway.httpPost(ep.port,
                                                mangaPath(d.source, d.comicId, "/download"), body)
                                            if (r.ok) pollTick++
                                            actionMsg = downloadStartMessage(
                                                r, "已创建下载任务：选中的 ${ids.size} 个单元（见下载页）")
                                            if (r.ok) selected = emptySet()
                                        }
                                    },
                                    modifier = Modifier.weight(1f).testTag("download_selected"),
                                ) { Text("下载选中（${selected.size} 话）") }
                                OutlinedButton(
                                    onClick = { selected = emptySet() },
                                ) { Text("清空选择") }
                            }
                        }
                        Spacer(Modifier.height(6.dp))
                        Row(Modifier.fillMaxWidth().padding(horizontal = WnSpace.lg)) {
                            OutlinedButton(
                                onClick = { confirmRemove = true },
                                modifier = Modifier.weight(1f),
                            ) { Text("从书库移除") }
                        }
                        // 删除管理入口：已下载的话/整卷可勾选后删除（用户明确点名才删）
                        if (d.downloadedCount > 0) {
                            Spacer(Modifier.height(6.dp))
                            Row(Modifier.fillMaxWidth().padding(horizontal = WnSpace.lg),
                                horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                                OutlinedButton(
                                    onClick = {
                                        manageMode = !manageMode
                                        if (!manageMode) deleteSel = emptySet()
                                    },
                                    modifier = Modifier.weight(1f)
                                        .testTag("manage_downloaded"),
                                ) {
                                    Text(if (manageMode) "完成管理" else "管理已下载（选择删除）")
                                }
                                if (manageMode) {
                                    OutlinedButton(
                                        onClick = { if (deleteSel.isNotEmpty())
                                            confirmDelete = true },
                                        enabled = deleteSel.isNotEmpty() && !deleting,
                                        modifier = Modifier.weight(1f)
                                            .testTag("delete_selected"),
                                    ) {
                                        Text(if (deleting) "删除中…"
                                            else "删除选中（${deleteSel.size}）",
                                            color = WnColors.danger)
                                    }
                                }
                            }
                        }
                        actionMsg?.let {
                            Spacer(Modifier.height(6.dp))
                            Text(it, style = MaterialTheme.typography.labelMedium,
                                 color = MaterialTheme.colorScheme.primary,
                                 modifier = Modifier.padding(horizontal = WnSpace.lg))
                            if (it.startsWith("阅读历史暂不可读")) {
                                TextButton(onClick = { reload() }) { Text("重试读取历史") }
                            }
                        }
                        // 下载任务实时状态：章级/图级进度 + 暂停/继续（数据全部来自
                        // 服务端轮询，不自己估算；停止原因照实显示）
                        dl?.takeIf { it.present }?.let { st ->
                            Spacer(Modifier.height(6.dp))
                            WnHairlineCard(Modifier.padding(horizontal = WnSpace.lg)) {
                                Column(Modifier.padding(WnSpace.md)) {
                                    Text(
                                        when (st.status) {
                                            "running" -> "下载中：" + st.progressLabel
                                            "queued" -> "下载排队中…"
                                            "paused" -> "已暂停：" + st.progressLabel
                                            "stopped" -> "已停止：" + st.progressLabel
                                            "error" -> "下载失败：" + st.progressLabel
                                            else -> st.progressLabel
                                        },
                                        style = MaterialTheme.typography.labelMedium,
                                    )
                                    if (st.imagesTotal > 0) {
                                        Spacer(Modifier.height(WnSpace.xs))
                                        LinearProgressIndicator(
                                            progress = {
                                                (st.imagesDone.toFloat() /
                                                    st.imagesTotal.coerceAtLeast(1))
                                                    .coerceIn(0f, 1f)
                                            },
                                            modifier = Modifier.fillMaxWidth(),
                                        )
                                    }
                                    if (st.stopReason.isNotBlank()) {
                                        Spacer(Modifier.height(WnSpace.xs))
                                        Text(st.stopReason,
                                            style = MaterialTheme.typography.labelSmall,
                                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                                    }
                                    if (st.active || st.resumable) {
                                        Spacer(Modifier.height(WnSpace.sm))
                                        Row(horizontalArrangement =
                                                Arrangement.spacedBy(WnSpace.sm)) {
                                            if (st.active) {
                                                OutlinedButton(
                                                    enabled = !dlBusy,
                                                    onClick = {
                                                        dlBusy = true
                                                        scope.launch {
                                                            gateway.httpPost(ep.port,
                                                                "/api/manga/download/pause" +
                                                                    "?source=${Uri.encode(source)}" +
                                                                    "&cid=${Uri.encode(comicId)}")
                                                            dl = fetchDl()
                                                            dlBusy = false
                                                        }
                                                    },
                                                    modifier = Modifier.testTag("dl_pause"),
                                                ) { Text("暂停下载") }
                                            }
                                            if (st.resumable) {
                                                OutlinedButton(
                                                    enabled = !dlBusy,
                                                    onClick = {
                                                        dlBusy = true
                                                        scope.launch {
                                                            val r = gateway.httpPost(ep.port,
                                                                "/api/manga/download/resume" +
                                                                    "?source=${Uri.encode(source)}" +
                                                                    "&cid=${Uri.encode(comicId)}")
                                                            actionMsg = if (r.ok) {
                                                                pollTick++
                                                                "已请求继续下载"
                                                            } else {
                                                                "继续下载未启动：${r.body.take(100)}"
                                                            }
                                                            dlBusy = false
                                                        }
                                                    },
                                                    modifier = Modifier.testTag("dl_resume"),
                                                ) { Text("继续下载") }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        Spacer(Modifier.height(8.dp))
                        HorizontalDivider()
                    }
                    // 整卷排在最前面：是可下载单元（勾选后随下载任务走）；
                    // 阅读器按话打开，整卷行点击 = 勾选，不进阅读器
                    if (d.volumes.isNotEmpty()) {
                        item {
                            Text("整卷（${d.volumes.size}）",
                                style = MaterialTheme.typography.titleSmall,
                                fontWeight = FontWeight.Bold,
                                modifier = Modifier.padding(start = WnSpace.lg,
                                    top = WnSpace.md))
                        }
                        itemsIndexed(d.volumes, key = { _, v -> "vol_" + v.id }) { vi, v ->
                            val vDownloaded = d.downloaded.contains(v.id)
                            val vInDl = dl?.chapterIds?.contains(v.id) == true
                            val vActive = dl?.active == true
                            val vCheckable = if (manageMode) vDownloaded
                                else !vDownloaded && !(vActive && vInDl)
                            Row(
                                Modifier.fillMaxWidth()
                                    .clickable {
                                        if (manageMode) {
                                            if (vDownloaded) deleteSel =
                                                if (deleteSel.contains(v.id))
                                                    deleteSel - v.id else deleteSel + v.id
                                        } else if (vCheckable) {
                                            selected = if (selected.contains(v.id))
                                                selected - v.id else selected + v.id
                                        }
                                    }
                                    .padding(horizontal = WnSpace.lg,
                                        vertical = WnSpace.md),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                if (vCheckable) {
                                    Checkbox(
                                        checked = if (manageMode) deleteSel.contains(v.id)
                                                  else selected.contains(v.id),
                                        onCheckedChange = { on ->
                                            if (manageMode) deleteSel =
                                                if (on) deleteSel + v.id else deleteSel - v.id
                                            else selected =
                                                if (on) selected + v.id else selected - v.id
                                        },
                                        modifier = Modifier.testTag("pick_${v.id}"),
                                    )
                                } else {
                                    Spacer(Modifier.width(48.dp))
                                }
                                Text("卷${vi + 1}",
                                    style = MaterialTheme.typography.labelMedium,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    modifier = Modifier.width(40.dp))
                                Text(v.label, style = MaterialTheme.typography.bodyMedium,
                                    maxLines = 1, overflow = TextOverflow.Ellipsis,
                                    modifier = Modifier.weight(1f))
                                Text(
                                    when {
                                        vActive && vInDl && vDownloaded -> "下载中"
                                        vActive && vInDl -> "排队中"
                                        vDownloaded -> "已下载"
                                        else -> "整卷"
                                    },
                                    style = MaterialTheme.typography.labelSmall,
                                    color = when {
                                        vActive && vInDl && vDownloaded -> WnColors.accent
                                        vActive && vInDl ->
                                            MaterialTheme.colorScheme.onSurfaceVariant
                                        vDownloaded ->
                                            MaterialTheme.colorScheme.onSurfaceVariant
                                        else -> WnColors.accent
                                    },
                                )
                                if (!manageMode) {
                                    TextButton(onClick = {
                                        val idx = d.readingChapters.indexOfFirst { it.id == v.id }
                                        if (idx >= 0) onRead(idx, 1, true, "", v.id, v.label)
                                    }) { Text("阅读") }
                                }
                            }
                            HorizontalDivider(color = WnColors.line)
                        }
                    }
                    itemsIndexed(d.chapters) { i, c ->
                        // 卷/话组标题：只在分组变化处显示一次（服务端返回 group 字段）
                        if (c.group.isNotBlank() &&
                            (i == 0 || d.chapters[i - 1].group != c.group)) {
                            Text(
                                c.group,
                                style = MaterialTheme.typography.titleSmall,
                                fontWeight = FontWeight.Bold,
                                modifier = Modifier.padding(start = WnSpace.lg, top = WnSpace.md),
                            )
                        }
                        val downloaded = d.downloaded.contains(c.id)
                        val partialDownloaded = d.partialDownloaded.contains(c.id)
                        // 在下载任务里的话：有部分图片（被计为已下载）=「下载中」，
                        // 还没轮到 =「排队中」——不能再笼统显示「已下载」
                        val inDlTask = dl?.chapterIds?.contains(c.id) == true
                        val dlActive = dl?.active == true
                        val chapterStatus = mangaChapterStatusLabel(
                            isReading = d.volumes.size + i == readIdx,
                            inDownloadTask = dlActive && inDlTask,
                            taskRunning = dl?.status == "running",
                            isCurrentDownloadChapter = dl?.currentChapterId == c.id,
                            downloaded = downloaded,
                            partiallyDownloaded = partialDownloaded,
                        )
                        // 管理模式可删除完整/部分本地内容；普通模式可下载或补齐未完成章节。
                        val rowCheckable = if (manageMode) downloaded || partialDownloaded
                            else !downloaded && !(dlActive && inDlTask)
                        Row(
                            Modifier.fillMaxWidth()
                                .clickable {
                                    if (manageMode) {
                                        // 管理模式：点行=勾选删除（不误进阅读器）
                                        if (downloaded || partialDownloaded) deleteSel =
                                            if (deleteSel.contains(c.id))
                                                deleteSel - c.id else deleteSel + c.id
                                    } else {
                                        // 点行=打开阅读；未下载的话同时用于勾选下载。
                                        // 这是"用户明确选了这一话"：不算近似落点，页码从 1 开始
                                        onRead(d.volumes.size + i, 1, true, "", c.id, c.label)
                                    }
                                }
                                .padding(horizontal = WnSpace.lg, vertical = WnSpace.md),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            // 未下载且不在进行中的任务里：可勾选（点方框只切换勾选，不打开阅读）
                            if (rowCheckable) {
                                Checkbox(
                                    checked = if (manageMode) deleteSel.contains(c.id)
                                              else selected.contains(c.id),
                                    onCheckedChange = { on ->
                                        if (manageMode) deleteSel =
                                            if (on) deleteSel + c.id else deleteSel - c.id
                                        else selected =
                                            if (on) selected + c.id else selected - c.id
                                    },
                                    modifier = Modifier.testTag("pick_${c.id}"),
                                )
                            } else {
                                Spacer(Modifier.width(48.dp))
                            }
                            Text("${i + 1}", style = MaterialTheme.typography.labelMedium,
                                 color = MaterialTheme.colorScheme.onSurfaceVariant,
                                 modifier = Modifier.width(40.dp))
                            Text(c.label, style = MaterialTheme.typography.bodyMedium,
                                 maxLines = 1, overflow = TextOverflow.Ellipsis,
                                 modifier = Modifier.weight(1f))
                            Text(
                                chapterStatus,
                                style = MaterialTheme.typography.labelSmall,
                                color = when {
                                    chapterStatus == "在读" -> MaterialTheme.colorScheme.primary
                                    chapterStatus == "补齐中" || chapterStatus == "下载中" ->
                                        WnColors.accent
                                    chapterStatus == "排队中" ->
                                        MaterialTheme.colorScheme.onSurfaceVariant
                                    chapterStatus == "部分下载 · 可补齐" -> WnColors.accent
                                    chapterStatus == "已下载" -> MaterialTheme.colorScheme.onSurfaceVariant
                                    else -> MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.6f)
                                },
                            )
                        }
                        HorizontalDivider(color = WnColors.line)
                    }
                }
            }
        }
    }
    // 删除选中话/整卷的确认弹窗：写清删什么、不删什么
    if (confirmDelete) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            title = { Text("删除选中的已下载内容？") },
            text = {
                Text("将删除选中的 ${deleteSel.size} 个话/整卷的已下载图片" +
                    "（本机文件删除后不可恢复；阅读进度与书库记录保留）。" +
                    "删除后这些章节需要联网才能重新下载。")
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        confirmDelete = false
                        deleting = true
                        scope.launch {
                            val body = org.json.JSONObject()
                                .put("chapter_ids",
                                    org.json.JSONArray(deleteSel.toList()))
                                .toString()
                            val r = gateway.httpPost(ep.port,
                                mangaPath(source, comicId, "/chapters/delete"), body)
                            if (r.ok) {
                                MangaReadCache.invalidate(source, comicId)
                                OfflineStore.invalidateMangaIndex(
                                    OfflineStore.runtimeDir(ctx), source, comicId)
                            }
                            val o = runCatching { org.json.JSONObject(r.body) }.getOrNull()
                            actionMsg = if (r.ok && o?.optBoolean("ok") == true) {
                                "已删除 ${o.optInt("deleted")} 项，释放 " +
                                    Export.human(o.optLong("freed_bytes", 0))
                            } else {
                                "删除失败：HTTP ${r.code} · " +
                                    (o?.optString("error")?.take(80) ?: r.body.take(80))
                            }
                            deleteSel = emptySet()
                            manageMode = false
                            deleting = false
                            reload()
                        }
                    },
                    modifier = Modifier.testTag("delete_confirm"),
                ) { Text("删除", color = WnColors.danger) }
            },
            dismissButton = {
                TextButton(onClick = { confirmDelete = false }) { Text("取消") }
            },
        )
    }

    if (confirmRemove) {
        AlertDialog(
            onDismissRequest = { confirmRemove = false },
            title = { Text("从书库移除？") },
            text = {
                Text(
                    "从书架移除只影响书架显示；已下载图片、阅读历史和收藏都会保留。\n" +
                        "删除下载图片请在「管理已下载」中单独选择章节删除，不会清除阅读历史或收藏。"
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmRemove = false
                    scope.launch {
                        // 注意：书库删除路由是 /api/manga/library/<source>/<comic_id>，
                        // 不是 /api/manga/<source>/<id>/library（早先写错会 404）
                        val r = gateway.httpDelete(ep.port,
                            "/api/manga/library/${Uri.encode(source)}/${Uri.encode(comicId)}")
                        actionMsg = if (r.ok) {
                            "已从书架移除，下载内容与阅读数据已保留：${detail?.title ?: comicId}"
                        } else "移除失败：HTTP ${r.code} ${r.body.take(80)}"
                        if (r.ok) onBack()
                    }
                }, modifier = Modifier.testTag("remove_keep_files")) { Text("确认移出书架") }
            },
            dismissButton = {
                Row {
                    TextButton(onClick = { confirmRemove = false }) { Text("取消") }
                }
            },
        )
    }
}

// 实时进度：两次自动保存之间的最小间隔（毫秒）。太小会疯狂打本机接口，
// 太大就"不实时"；2s 兼顾"翻页即记"与开销。
private const val PROGRESS_MIN_GAP_MS = 2000L
private const val READER_CONTROLS_IDLE_HIDE_MS = 4000L

/** Small reader-only hot zone: a deliberate center tap reveals controls without stealing page taps. */
internal fun isReaderControlHotZone(x: Float, y: Float, width: Float, height: Float): Boolean =
    x in (width * 0.39f)..(width * 0.61f) &&
        y in (height * 0.43f)..(height * 0.57f)

internal enum class MangaReaderKeyAction { PREVIOUS_PAGE, NEXT_PAGE, PREVIOUS_CHAPTER, NEXT_CHAPTER }

/** Keep hardware-key behavior aligned with the web reader without changing touch hit zones. */
internal fun mangaReaderKeyAction(keyCode: Int, paged: Boolean): MangaReaderKeyAction? = when (keyCode) {
    AndroidKeyEvent.KEYCODE_PAGE_UP -> MangaReaderKeyAction.PREVIOUS_PAGE
    AndroidKeyEvent.KEYCODE_PAGE_DOWN -> MangaReaderKeyAction.NEXT_PAGE
    AndroidKeyEvent.KEYCODE_MOVE_HOME -> MangaReaderKeyAction.PREVIOUS_CHAPTER
    AndroidKeyEvent.KEYCODE_MOVE_END -> MangaReaderKeyAction.NEXT_CHAPTER
    AndroidKeyEvent.KEYCODE_DPAD_LEFT ->
        if (paged) MangaReaderKeyAction.PREVIOUS_PAGE else null
    AndroidKeyEvent.KEYCODE_DPAD_RIGHT ->
        if (paged) MangaReaderKeyAction.NEXT_PAGE else null
    AndroidKeyEvent.KEYCODE_DPAD_UP ->
        if (!paged) MangaReaderKeyAction.PREVIOUS_PAGE else null
    AndroidKeyEvent.KEYCODE_DPAD_DOWN ->
        if (!paged) MangaReaderKeyAction.NEXT_PAGE else null
    else -> null
}

/** A chapter URL list is reusable only for the catalog mode that produced it. */
internal fun readerCacheMatchesCatalog(pages: MangaChapterPages, localCatalog: Boolean): Boolean =
    pages.local == localCatalog

// ── 原生漫画阅读器（纵向连续）────────────────────────────────

@Composable
fun MangaReaderScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    source: String,
    comicId: String,
    startChapterIndex: Int,
    startPage: Int,
    loader: coil.ImageLoader,
    onBack: () -> Unit,
    /**
     * 起始落点是否精确（服务端按章节身份解析，0.64.0）。false 时**禁用自动保存**：
     * 未精确定位就自动写盘，会把"上次读到第47话"覆盖成"第1话"——用户看到的就是
     * "刚读完一本，重进却让我从第一话重读"。
     */
    startTrusted: Boolean = true,
    resumeNote: String = "",
    /** 起始话的**身份**：在自己的列表里再定位一次（列表可能与详情页不同） */
    startChapterId: String = "",
    startLabel: String = "",
    localCatalog: Boolean = false,
) {
    val ctx = androidx.compose.ui.platform.LocalContext.current
    var detail by remember { mutableStateOf<MangaDetail?>(null) }
    var chapterIndex by rememberSaveable(source, comicId, localCatalog) {
        mutableIntStateOf(startChapterIndex.coerceAtLeast(0))
    }
    // The route contains the original launch position. After recreation, prefer the live
    // session position saved below instead of reopening that original chapter.
    var readerSessionStarted by rememberSaveable(source, comicId, localCatalog) {
        mutableStateOf(false)
    }
    var liveChapterId by rememberSaveable(source, comicId, localCatalog) {
        mutableStateOf(startChapterId)
    }
    var liveChapterLabel by rememberSaveable(source, comicId, localCatalog) {
        mutableStateOf(startLabel)
    }
    // 用户是否**自己**换过章（点目录/上一话/下一话）。近似落点下只有这个动作
    // 才算"用户选择了这一话"，才允许覆盖记录。
    var userNavigated by remember { mutableStateOf(startTrusted) }
    var saveNote by remember { mutableStateOf(resumeNote) }
    // 本屏实际解析出的起始话（可能与传入下标不同：按身份定位的结果）
    var startResolved by remember { mutableIntStateOf(startChapterIndex) }
    /** 本屏已进入（loadChapter 接管）的章节 id——自动保存的写入门槛 */
    var loadedChapterId by remember { mutableStateOf("") }
    var startPageResolved by remember { mutableIntStateOf(startPage.coerceAtLeast(1)) }
    // 页码也要跟着身份走：定位到的那一话才是"上次读到的位置"
    LaunchedEffect(startChapterId, startLabel) {
        if (startChapterId.isNotBlank() || startLabel.isNotBlank()) {
            // 传进来的页码属于"记录里那一话"，只有定位结果一致时才用它
            startPageResolved = startPage.coerceAtLeast(1)
        }
    }
    var pages by remember { mutableStateOf<MangaChapterPages?>(null) }
    // 旧版图片缓存提示（升级版 Venera 指南 §4 P0"旧缓存升级体验"）：服务端在章节
    // 元信息里带 stale_processing/stale_reason，界面要把它显示成"需要联网重新获取"，
    // 而不是让用户以为是自己手机解码坏了。
    var staleReason by remember { mutableStateOf("") }
    var rebuilding by remember { mutableStateOf(false) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var showToc by remember { mutableStateOf(false) }
    var controlsVisible by remember { mutableStateOf(false) }
    var readerSeekPage by remember { mutableIntStateOf(0) }
    var progressSliderDragging by remember { mutableStateOf(false) }
    var pageNo by rememberSaveable(source, comicId, localCatalog) {
        mutableIntStateOf(startPage.coerceAtLeast(1))
    }
    // 缩放：默认 1×（适应宽度）。只有放大后才接管拖动，否则保持正常滚动。
    var zoom by remember { mutableFloatStateOf(1f) }
    var panOffset by remember { mutableStateOf(Offset.Zero) }
    // 阅读方向：纵向连续 / 横向翻页（偏好落盘，下次进入沿用）
    var prefs by remember { mutableStateOf(ReaderPrefs.load(ctx)) }
    val paged = prefs.horizontalPaging

    // 阅读控件只在阅读画面内出现：触摸中心小热区后短暂显示，阅读继续/空闲后收起。
    // 打开目录、拖动进度条、章节/页码/缩放发生变化时重置计时；加载错误状态不抢用户操作。
    LaunchedEffect(
        controlsVisible, showToc, progressSliderDragging, pageNo, chapterIndex,
        zoom, readerSeekPage, paged, loading, error, pages,
    ) {
        if (!controlsVisible || showToc || progressSliderDragging || loading || error != null || pages == null) {
            return@LaunchedEffect
        }
        delay(READER_CONTROLS_IDLE_HIDE_MS)
        controlsVisible = false
    }
    // 已读过的章节页数缓存：来回翻章不重复请求
    val cache = remember { mutableStateMapOf<String, MangaChapterPages>() }
    // 页加载失败 → 就地**重试**（§8.C"图片失败重试"）：计数器进 ImageRequest 参数，
    // 参与 Coil 的缓存键 → 点一次就真的重新拉这张图，不必退出章节再进来。
    val retryTick = remember { mutableStateMapOf<String, Int>() }
    // 注意：d / ch 只在渲染分支里可见，所以这两个助手显式接参（不靠闭包捕获）
    fun pageRequest(src: String, cid: String, chapterId: String,
                    p: MangaChapterPages?, i: Int): ImageRequest {
        val tick = retryTick["$chapterId:$i"] ?: 0
        val url = resolveMangaPageUrl(ep.imagePort, src, cid, chapterId,
            p?.entries?.getOrNull(i), i, tick, localCatalog)
        return ImageRequest.Builder(ctx)
            .data(url)
            .memoryCacheKey(url)
            .diskCacheKey(url)
            .crossfade(false)
            .build()
    }

    fun bumpRetry(chapterId: String, i: Int) {
        retryTick["$chapterId:$i"] = (retryTick["$chapterId:$i"] ?: 0) + 1
    }
    val scope = rememberCoroutineScope()
    val bg = WnColors.bg
    // 预取慢速通道（2 并发、小内存缓存）：与可见页的交互通道（4 并发）隔离，
    // 后台预取永远抢不到可见页的引擎线程，也不会形成请求风暴。
    val prefetchLoader = remember(ep) {
        engineImageLoader(ctx, ep, maxPerHost = 3, smallMemoryCache = true)
    }
    DisposableEffect(prefetchLoader) {
        onDispose { releaseEngineImageLoader(prefetchLoader) }
    }

    /**
     * 保存阅读进度。
     *
     * 0.64.0：除 `idx`/`pos` 外还要记**章节身份**（id + 章名）。只记下标的话，
     * 目录一变（加更/删章/插番外）下标就指向别的一话，续读会落到错误章节。
     */
    suspend fun saveProgress(chIdx: Int, page: Int): Boolean {
        val d = detail ?: return false
        val ch = d.readingChapters.getOrNull(chIdx) ?: return false
        // 只写"**本屏正在读**的那一话"：以"本章是否已被 loadChapter 接管"为准，
        // 而不是"图片是否加载成功"。
        //   · 为什么不能用 pages：本章图片取不到（例如刚重建过缓存、源站临时失败）时
        //     pages 为 null，用户明明停在这一话，却连"读到这一话"都不记——
        //     实测被 StaleImageBannerTest 抓出（详情页永远停在"开始阅读"）。
        //   · 为什么仍需门槛：切章过程中 chapterIndex 短暂指向别的话时，
        //     自动保存会把错误的一话写进记录（网页端就是这么把记录冲成"第1话 P1"的）。
        if (loadedChapterId != ch.id) return false
        // 近似落点 + 用户还没自己换过章 → 不覆盖用户的记录（并把原因显示出来）
        if (!userNavigated) {
            saveNote = resumeNote.ifBlank {
                "这次没能精确定位到上次读到的那一话，已暂停自动记录，避免覆盖你的进度"
            }
            return false
        }
        val body = JSONObject()
            .put("source", d.source).put("comic_id", d.comicId)
            .put("idx", chIdx)
            .put("chapter_id", ch.id)
            .put("chapter_label", ch.label)
            .put("pos", EngineData.mangaPos(ch.label, page))
            .put("title", d.title)
            .toString()
        val r = gateway.httpPost(ep.port, "/api/manga/history", body)
        return r.ok && !r.body.contains("\"ok\": false") && !r.body.contains("\"ok\":false")
    }

    suspend fun loadChapter(idx: Int) {
        val d = detail ?: return
        val ch = d.readingChapters.getOrNull(idx) ?: return
        val preservePosition = readerSessionStarted && idx == chapterIndex
        val requestedPage = if (preservePosition) pageNo
            else if (idx == startResolved) startPageResolved else 1
        readerSessionStarted = true
        liveChapterId = ch.id
        liveChapterLabel = ch.label
        // 本屏"进入"这一话：此后即使图片取不到，也允许把"读到这一话"记下来
        loadedChapterId = ch.id
        loading = true; error = null
        val hit = cache[ch.id]?.takeIf { readerCacheMatchesCatalog(it, localCatalog) }
            ?: MangaReadCache.getUrls(d.source, d.comicId, ch.id, localCatalog)
        if (hit != null) {
            cache[ch.id] = hit
            pages = hit
            chapterIndex = idx
            pageNo = requestedPage.coerceIn(1, hit.count.coerceAtLeast(1))
            loading = false
            return
        }
        // 批量接口 /urls（与网页端同一路径）：一次给出每页加载方式（本地直出/
        // 懒下载通道/CDN 直连），服务端同时启动本章流水线预热。失败自动重试 1 次
        // （对齐网页端 45s 超时×2：copymanga 间歇风控时"首次失败、手动再点才成功"
        // 的抖动大多能自己缓过来）。
        val localSuffix = if (localCatalog) "?catalog=local" else ""
        var r = gateway.httpText(ep.port,
            mangaPath(d.source, d.comicId, "/chapter/${Uri.encode(ch.id)}/urls") + localSuffix)
        var p = if (r.ok) EngineData.mangaChapterUrls(r.body) else null
        if (p == null || p.count <= 0) {
            delay(1500)
            r = gateway.httpText(ep.port,
                mangaPath(d.source, d.comicId, "/chapter/${Uri.encode(ch.id)}/urls") + localSuffix)
            p = if (r.ok) EngineData.mangaChapterUrls(r.body) else null
        }
        if (p == null || p.count <= 0) {
            pages = null
            error = "本话暂时取不到图片：HTTP ${r.code}" +
                if (r.body.isNotBlank()) " · ${r.body.take(120)}" else ""
        } else {
            cache[ch.id] = p
            MangaReadCache.putUrls(d.source, d.comicId, ch.id, p, localCatalog)
            pages = p
            staleReason = if (p.staleProcessing) {
                p.staleReason.ifBlank { "本章图片是旧版处理缓存，需联网重新获取" }
            } else ""
            chapterIndex = idx
            pageNo = requestedPage.coerceIn(1, p.count.coerceAtLeast(1))
        }
        loading = false
    }

    // 同作品的完整/本地目录是两种不同阅读会话；切换时取消旧加载并取对应目录。
    LaunchedEffect(source, comicId, localCatalog) {
        // 详情缓存命中（详情页预载）→ 零往返直接开章节；否则联网取，失败自动重试 1 次
        var lastCode = 0
        val cached = MangaReadCache.getDetail(source, comicId, localCatalog)
        if (cached != null) {
            detail = cached
        } else {
            // 失败重试同样只在快速失败时（<12s）；超时链路重试等于把等待翻倍
            val t0 = System.currentTimeMillis()
            val catalogSuffix = if (localCatalog) "?catalog=local" else ""
            var r = gateway.httpText(ep.port, mangaPath(source, comicId, catalogSuffix))
            lastCode = r.code
            var d = if (r.ok) EngineData.mangaDetail(r.body) else null
            if (d == null && System.currentTimeMillis() - t0 < 12_000) {
                delay(1500)
                r = gateway.httpText(ep.port, mangaPath(source, comicId, catalogSuffix))
                lastCode = r.code
                d = if (r.ok) EngineData.mangaDetail(r.body) else null
            }
            detail = d
            if (d != null) MangaReadCache.putDetail(source, comicId, d, localCatalog)
        }
        val d = detail
        if (d == null) {
            loading = false
            error = "读取目录失败" + if (lastCode != 0) "：HTTP $lastCode" else ""
            return@LaunchedEffect
        }
        // 0.64.0：起始话**在本屏自己的列表里按身份重新定位**。详情页与阅读器
        // 各自取详情（一个可能来自下载缓存、一个是源站最新），只传下标会在两份
        // 列表不一致时打开别的一话——用户看到的就是"继续阅读打开的又不是记录里那一话"。
        val idx = if (readerSessionStarted) {
            MangaResumeResolver.resolve(
                d.readingChapters, liveChapterId, liveChapterLabel, chapterIndex)
        } else MangaResumeResolver.resolve(
            d.readingChapters, startChapterId, startLabel, startChapterIndex)
        startResolved = idx
        loadChapter(if (idx >= 0) idx else startChapterIndex.coerceAtLeast(0))
    }

    // 系统返回（手势/返回键）与顶栏返回按钮必须同样保存进度：此前只有顶栏按钮
    // 保存，用手势返回就丢进度——"刚读完退出再进又让我从第一话读"的一个来源。
    // 保存**限时等待 1.5s**：引擎线程被图片回源占满时，无界等待会把退出冻住
    // （实测"漫画加载时无法返回"即此）。超时照样退出——阅读中每 2s 的领先写
    // + 停稳 1.2s 的尾写几乎总已落盘，不会丢进度。
    BackHandler {
        scope.launch {
            kotlinx.coroutines.withTimeoutOrNull(1500) {
                saveProgress(chapterIndex, pageNo)
            }
            onBack()
        }
    }

    Scaffold(
        topBar = {
            if (controlsVisible) {
            TopAppBar(
                title = {
                    val d = detail
                    val ch = d?.readingChapters?.getOrNull(chapterIndex)
                    Text(
                        listOfNotNull(d?.title, ch?.label).joinToString(" · ").ifBlank { "漫画阅读" },
                        maxLines = 1, overflow = TextOverflow.Ellipsis,
                        style = MaterialTheme.typography.titleMedium,
                    )
                },
                navigationIcon = {
                    IconButton(onClick = {
                        scope.launch {
                            kotlinx.coroutines.withTimeoutOrNull(1500) {
                                saveProgress(chapterIndex, pageNo)
                            }
                            onBack()
                        }
                    }) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                    }
                },
                actions = {
                    TextButton(onClick = {
                        controlsVisible = false
                        showToc = true
                    }) { Text("目录") }
                    TextButton(onClick = {
                        val next = prefs.copy(horizontalPaging = !prefs.horizontalPaging)
                        prefs = next
                        ReaderPrefs.save(ctx, next)
                    }) { Text(if (paged) "连续" else "翻页") }
                    // 点按=双击的等价入口（更好发现，也便于自动验收）
                    TextButton(onClick = {
                        if (zoom > 1f) { zoom = 1f; panOffset = Offset.Zero } else { zoom = 2f }
                    }) { Text(if (zoom > 1f) "复位" else "放大") }
                },
            )
            }
        },
        bottomBar = {
            if (controlsVisible) {
            Column {
                HorizontalDivider(color = WnColors.line)
                Surface(color = WnColors.surface) {
                    Column(Modifier.fillMaxWidth().padding(horizontal = WnSpace.sm,
                        vertical = WnSpace.xs)) {
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            TextButton(
                                onClick = {
                                    userNavigated = true
                                    scope.launch { saveProgress(chapterIndex, pageNo); loadChapter(chapterIndex - 1) }
                                },
                                enabled = chapterIndex > 0,
                            ) { Text("上一话") }
                            Text(
                                buildString {
                                    append(chapterIndex + 1).append(" / ")
                                        .append(detail?.readingChapters?.size ?: 0)
                                    pages?.let { append(" · $pageNo/").append(it.count).append("页") }
                                    if (zoom > 1f) {
                                        append(" · 缩放 ")
                                        append(String.format(java.util.Locale.US, "%.1f", zoom))
                                        append("×")
                                    }
                                },
                                color = WnColors.ink,
                                style = MaterialTheme.typography.labelMedium,
                                textAlign = TextAlign.Center,
                                modifier = Modifier.weight(1f).testTag("reader_progress_label"),
                            )
                            TextButton(
                                onClick = {
                                    userNavigated = true
                                    scope.launch { saveProgress(chapterIndex, pageNo); loadChapter(chapterIndex + 1) }
                                },
                                enabled = chapterIndex + 1 < (detail?.readingChapters?.size ?: 0),
                            ) { Text("下一话") }
                        }
                        val pageCount = pages?.count ?: 0
                        if (pageCount > 1) {
                            Slider(
                                value = (pageNo - 1).coerceIn(0, pageCount - 1).toFloat(),
                                onValueChange = { value ->
                                    val target = value.roundToInt().coerceIn(0, pageCount - 1) + 1
                                    progressSliderDragging = true
                                    pageNo = target
                                    readerSeekPage = target
                                },
                                onValueChangeFinished = { progressSliderDragging = false },
                                valueRange = 0f..(pageCount - 1).coerceAtLeast(1).toFloat(),
                                steps = (pageCount - 2).coerceAtLeast(0),
                                modifier = Modifier.fillMaxWidth().testTag("reader_progress_slider"),
                            )
                        }
                    }
                }
            }
            }
        },
    ) { pad ->
        Box(Modifier.fillMaxSize().background(bg)) {
            val d = detail
            val p = pages
            when {
                loading -> Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
                error != null -> Column(Modifier.fillMaxSize().padding(WnSpace.xl), Arrangement.Center) {
                    Text(error!!, color = MaterialTheme.colorScheme.error, textAlign = TextAlign.Center)
                    Spacer(Modifier.height(WnSpace.md))
                    Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                        Button(onClick = { scope.launch { loadChapter(chapterIndex) } }) { Text("重试") }
                        OutlinedButton(onClick = {
                            cache.remove(d?.chapters?.getOrNull(chapterIndex)?.id ?: "")
                            scope.launch { loadChapter(chapterIndex) }
                        }) { Text("重新拉取") }
                    }
                    if (p != null && p.count > 0) {
                        Spacer(Modifier.height(WnSpace.sm))
                        Text("本话已有 ${p.count} 页", color = WnColors.inkDim,
                             style = MaterialTheme.typography.bodySmall)
                    }
                }
                d == null || p == null -> Box(Modifier.fillMaxSize(), Alignment.Center) {
                    Text("没有可显示的内容", color = WnColors.inkDim)
                }
                else -> key(d.readingChapters[chapterIndex].id, paged) {
                    val ch = d.readingChapters[chapterIndex]
                    // 切换连续/翻页模式会重建视图状态，但阅读位置属于章节而不是
                    // 某种视图：必须从当前实时页码恢复，不能退回进入阅读器时的落点。
                    val initial = (pageNo - 1).coerceIn(0, (p.count - 1).coerceAtLeast(0))
                    val listState = rememberLazyListState(initialFirstVisibleItemIndex = initial)
                    val pagerState = rememberPagerState(initialPage = initial) { p.count }
                    val readerFocusRequester = remember(ch.id) { FocusRequester() }
                    LaunchedEffect(readerFocusRequester) {
                        readerFocusRequester.requestFocus()
                    }
                    LaunchedEffect(readerSeekPage, ch.id, paged) {
                        if (readerSeekPage <= 0) return@LaunchedEffect
                        val target = (readerSeekPage - 1).coerceIn(0, p.count - 1)
                        if (paged) pagerState.animateScrollToPage(target)
                        else listState.animateScrollToItem(target)
                        userNavigated = true
                        saveProgress(chapterIndex, target + 1)
                        readerSeekPage = 0
                    }
                    LaunchedEffect(ch.id, p.count, paged) {
                        // 两种模式共用同一套进度保存：连续模式看首个可见项，翻页模式看当前页
                        val flow = if (paged) snapshotFlow { pagerState.currentPage }
                                   else snapshotFlow { listState.firstVisibleItemIndex }
                        // 实时记录（用户要求"应该实时记录阅读进度"）：位置一变就**先写一次**
                        // （两次写之间至少间隔 2s），停稳后再补一次尾写。
                        // 旧实现是 collectLatest + delay(1200)：连续滚动时每次都被取消，
                        // 一路都在"等停"，只有停顿那一刻才真正落盘，中途卡死/被杀就丢进度。
                        var lastAt = 0L
                        flow.collect { pos ->
                            if (!progressSliderDragging) {
                                pageNo = (pos + 1).coerceIn(1, p.count)
                                val now = System.currentTimeMillis()
                                if (now - lastAt >= PROGRESS_MIN_GAP_MS) {
                                    lastAt = now
                                    saveProgress(chapterIndex, pageNo)
                                }
                            }
                        }
                    }
                    // 换章时把缩放复位，避免上一话的放大状态带到新的一话
                    // 尾写：位置停稳 1.2s 后再写一次（领先写被 2s 间隔挡掉时靠它兜底）
                    LaunchedEffect(ch.id, pageNo, chapterIndex) {
                        delay(1200)
                        saveProgress(chapterIndex, pageNo)
                    }
                    // 切后台/锁屏也要保存（Android 可能在之后随时冻结进程）
                    val lifecycleOwner = androidx.lifecycle.compose.LocalLifecycleOwner.current
                    DisposableEffect(lifecycleOwner) {
                        val obs = androidx.lifecycle.LifecycleEventObserver { _, ev ->
                            if (ev == androidx.lifecycle.Lifecycle.Event.ON_STOP) {
                                scope.launch { saveProgress(chapterIndex, pageNo) }
                            }
                        }
                        lifecycleOwner.lifecycle.addObserver(obs)
                        onDispose { lifecycleOwner.lifecycle.removeObserver(obs) }
                    }
                    LaunchedEffect(ch.id) { zoom = 1f; panOffset = Offset.Zero }
                    // 后台整话预取：从当前页开始，2 条慢速通道顺序拉完本话剩余页。
                    // execute 自 pacing——源站多快就多快、最多 2 个在途：快源几秒
                    // 拉完一话，被限速的源自动低速跟进而不是硬顶（16 并发触发风控
                    // 的教训）；换话时 LaunchedEffect 自动取消。
                    LaunchedEffect(ch.id, p.count, localCatalog) {
                        val start = (pageNo - 1).coerceIn(0, (p.count - 1).coerceAtLeast(0))
                        listOf(0, 1).forEach { lane ->
                            launch {
                                var i = start + lane
                                while (i < p.count) {
                                    runCatching {
                                        prefetchLoader.execute(
                                            pageRequest(d.source, d.comicId, ch.id, p, i))
                                    }
                                    i += 2
                                }
                            }
                        }
                    }
                    // 临近话尾（剩 ≤3 页）：经同一慢速通道预取下一话元数据（进本屏
                    // 页数缓存，翻话零等待）+ 前 4 页。有界：1 话元数据 + 4 页、
                    // 2 并发——提速靠通道隔离与自 pacing，不靠堆量。
                    val approachingChapterEnd = p.count - pageNo <= 3
                    LaunchedEffect(ch.id, p.count, approachingChapterEnd, localCatalog) {
                        // 不把 pageNo 本身作为 effect key：尾部连续翻页时目录请求与
                        // 图片预取应保持在途，不能每翻一页就取消并重新请求一次。
                        if (!approachingChapterEnd) return@LaunchedEffect
                        val nextCh = d.readingChapters.getOrNull(chapterIndex + 1)
                            ?: return@LaunchedEffect
                        if (cache.containsKey(nextCh.id)) return@LaunchedEffect
                        val r = gateway.httpText(ep.port,
                            mangaPath(d.source, d.comicId,
                                "/chapter/${Uri.encode(nextCh.id)}/urls") +
                                if (localCatalog) "?catalog=local" else "")
                        val np = if (r.ok) EngineData.mangaChapterUrls(r.body) else null
                        if (np != null && np.count > 0) {
                            cache[nextCh.id] = np
                            MangaReadCache.putUrls(d.source, d.comicId, nextCh.id, np, localCatalog)
                            for (i in 0..minOf(3, np.count - 1)) {
                                runCatching {
                                    prefetchLoader.execute(
                                        pageRequest(d.source, d.comicId, nextCh.id, np, i))
                                }
                            }
                        }
                    }
                    val transformState = rememberTransformableState { zoomChange, panChange, _ ->
                        val next = (zoom * zoomChange).coerceIn(1f, 5f)
                        zoom = next
                        panOffset = if (next > 1f) panOffset + panChange else Offset.Zero
                    }
                    Box(
                        Modifier.fillMaxSize()
                            .focusRequester(readerFocusRequester)
                            .onPreviewKeyEvent { event ->
                                if (event.type != KeyEventType.KeyDown) return@onPreviewKeyEvent false
                                val action = mangaReaderKeyAction(
                                    event.nativeKeyEvent.keyCode, paged,
                                ) ?: return@onPreviewKeyEvent false
                                userNavigated = true
                                scope.launch {
                                    when (action) {
                                        MangaReaderKeyAction.PREVIOUS_PAGE,
                                        MangaReaderKeyAction.NEXT_PAGE -> {
                                            val forward = action == MangaReaderKeyAction.NEXT_PAGE
                                            if (paged) {
                                                val target = pagerState.currentPage + if (forward) 1 else -1
                                                pagerState.animateScrollToPage(target.coerceIn(0, p.count - 1))
                                            } else {
                                                val visibleCount = listState.layoutInfo.visibleItemsInfo.size
                                                    .coerceAtLeast(1)
                                                val step = (visibleCount - 1).coerceAtLeast(1)
                                                val target = listState.firstVisibleItemIndex +
                                                    if (forward) step else -step
                                                listState.animateScrollToItem(target.coerceIn(0, p.count - 1))
                                            }
                                        }
                                        MangaReaderKeyAction.PREVIOUS_CHAPTER -> {
                                            if (chapterIndex > 0) {
                                                saveProgress(chapterIndex, pageNo)
                                                loadChapter(chapterIndex - 1)
                                            }
                                        }
                                        MangaReaderKeyAction.NEXT_CHAPTER -> {
                                            if (chapterIndex + 1 < d.readingChapters.size) {
                                                saveProgress(chapterIndex, pageNo)
                                                loadChapter(chapterIndex + 1)
                                            }
                                        }
                                    }
                                }
                                true
                            }
                            .focusable()
                            .graphicsLayer {
                                scaleX = zoom; scaleY = zoom
                                translationX = panOffset.x; translationY = panOffset.y
                            }
                            // 只有放大状态才接管手势，否则让 LazyColumn 正常滚动
                            .transformable(state = transformState, enabled = zoom > 1f)
                            .pointerInput(ch.id, paged) {
                                detectTapGestures(onTap = { offset ->
                                    // Horizontal pages own single-tap behavior below, avoiding a
                                    // parent/child double toggle in the deliberately small hit zone.
                                    if (!paged && isReaderControlHotZone(
                                            offset.x, offset.y,
                                            size.width.toFloat(), size.height.toFloat())) {
                                        controlsVisible = !controlsVisible
                                    }
                                }, onDoubleTap = {
                                    if (zoom > 1f) { zoom = 1f; panOffset = Offset.Zero } else { zoom = 2f }
                                })
                            },
                    ) {
                    if (paged) {
                        // 横向翻页：一屏一页，整页可见（ContentScale.Fit），左右滑动翻页
                        HorizontalPager(
                            state = pagerState,
                            modifier = Modifier.fillMaxSize().testTag("manga_pager"),
                        ) { i ->
                            Box(
                                Modifier.fillMaxSize().pointerInput(ch.id, i) {
                                    detectTapGestures { offset ->
                                        val w = size.width
                                        when {
                                            offset.x < w / 3f -> scope.launch {
                                                pagerState.animateScrollToPage((i - 1).coerceAtLeast(0))
                                            }
                                            offset.x > w * 2f / 3f -> scope.launch {
                                                pagerState.animateScrollToPage(
                                                    (i + 1).coerceAtMost(p.count - 1))
                                            }
                                            else -> if (isReaderControlHotZone(
                                                    offset.x, offset.y,
                                                    size.width.toFloat(), size.height.toFloat())) {
                                                controlsVisible = !controlsVisible
                                            }
                                        }
                                    }
                                },
                            ) {
SubcomposeAsyncImage(
                                model = pageRequest(d.source, d.comicId, ch.id, p, i),
                                imageLoader = loader,
                                contentDescription = "第 ${i + 1} 页",
                                contentScale = ContentScale.Fit,
                                modifier = Modifier.fillMaxSize().background(WnColors.bg),
                                loading = {
                                    Box(Modifier.fillMaxSize(), Alignment.Center) {
                                        CircularProgressIndicator(strokeWidth = 2.dp)
                                    }
                                },
                                error = {
                                    // 失败自愈：自动退避重试 2 次（3s/8s）——多数"失败"是源站
                                    // 临时限速，自己缓过来比请求洪水有效；手动按钮保持可见。
                                    val autoRetried = remember(ch.id, i) { mutableIntStateOf(0) }
                                    if (autoRetried.intValue < 2) {
                                        LaunchedEffect(autoRetried.intValue) {
                                            delay(if (autoRetried.intValue == 0) 3000 else 8000)
                                            autoRetried.intValue++
                                            bumpRetry(ch.id, i)
                                        }
                                    }
                                    Column(Modifier.fillMaxSize(),
                                        verticalArrangement = Arrangement.Center,
                                        horizontalAlignment = Alignment.CenterHorizontally) {
                                        Text(
                                            "第 ${i + 1} 页加载失败" +
                                                if (autoRetried.intValue < 2) "，自动重试中…" else "",
                                            color = WnColors.danger,
                                            style = MaterialTheme.typography.labelSmall,
                                            textAlign = TextAlign.Center,
                                        )
                                        Spacer(Modifier.height(6.dp))
                                        OutlinedButton(
                                            onClick = { bumpRetry(ch.id, i) },
                                            modifier = Modifier.testTag("manga_page_retry_$i"),
                                        ) { Text("重试这一页") }
                                    }
                                },
                            )
                            }   // 点按翻页 Box 结束
                        }
                    } else {
                    LazyColumn(
                        state = listState,
                        modifier = Modifier.fillMaxSize().testTag("manga_pages"),
                        contentPadding = PaddingValues(bottom = 12.dp),
                    ) {
                        items(p.count) { i ->
                            SubcomposeAsyncImage(
                                model = pageRequest(d.source, d.comicId, ch.id, p, i),
                                imageLoader = loader,
                                contentDescription = "第 ${i + 1} 页",
                                contentScale = ContentScale.FillWidth,
                                // 不强制宽高比：页面按**图片原始比例**排版。
                                // 以前写死 aspectRatio(0.7f)，长图会被裁掉一截（实测比例断言已覆盖）。
                                modifier = Modifier.fillMaxWidth()
                                    .background(WnColors.bg),
                                loading = {
                                    // 占位用常见页面比例，避免加载中高度为 0 造成跳动
                                    Box(Modifier.fillMaxWidth().aspectRatio(0.7f), Alignment.Center) {
                                        CircularProgressIndicator(strokeWidth = 2.dp)
                                    }
                                },
                                error = {
                                    // 失败自愈：自动退避重试 2 次（3s/8s），手动按钮保持可见
                                    val autoRetried = remember(ch.id, i) { mutableIntStateOf(0) }
                                    if (autoRetried.intValue < 2) {
                                        LaunchedEffect(autoRetried.intValue) {
                                            delay(if (autoRetried.intValue == 0) 3000 else 8000)
                                            autoRetried.intValue++
                                            bumpRetry(ch.id, i)
                                        }
                                    }
                                    Column(Modifier.fillMaxWidth().padding(8.dp),
                                        verticalArrangement = Arrangement.Center,
                                        horizontalAlignment = Alignment.CenterHorizontally) {
                                        Text(
                                            "第 ${i + 1} 页加载失败" +
                                                if (autoRetried.intValue < 2) "，自动重试中…" else "",
                                            color = WnColors.danger,
                                            style = MaterialTheme.typography.labelSmall,
                                            textAlign = TextAlign.Center,
                                        )
                                        Spacer(Modifier.height(6.dp))
                                        OutlinedButton(
                                            onClick = { bumpRetry(ch.id, i) },
                                            modifier = Modifier.testTag("manga_page_retry_$i"),
                                        ) { Text("重试这一页") }
                                    }
                                },
                            )
                        }
                        item {
                            Spacer(Modifier.height(16.dp))
                            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
                                OutlinedButton(
                                    onClick = {
                                        userNavigated = true
                                        scope.launch { saveProgress(chapterIndex, pageNo); loadChapter(chapterIndex + 1) }
                                    },
                                    enabled = chapterIndex + 1 < d.readingChapters.size,
                                ) { Text("下一话") }
                            }
                            Spacer(Modifier.height(24.dp))
                        }
                    }
                    }   // 横向翻页分支 else 结束：LazyColumn（纵向连续）
                    }   // 缩放层 Box 结束
                }
            }

            // 续读定位说明（0.64.0）：服务端按章节身份定位失败/近似时会给出说明，
            // 必须显示出来——否则用户只会看到"怎么又从头开始了"，无从判断。
            if (saveNote.isNotBlank() && !loading) {
                Surface(
                    color = WnColors.surface,
                    border = BorderStroke(1.dp, WnColors.accent),
                    modifier = Modifier.fillMaxWidth().align(Alignment.TopCenter)
                        .testTag("manga_resume_note_banner"),
                ) {
                    Text(
                        saveNote,
                        color = WnColors.ink,
                        style = MaterialTheme.typography.labelSmall,
                        modifier = Modifier.padding(horizontal = WnSpace.md, vertical = 6.dp),
                    )
                }
            }
            // 旧版图片缓存提示条：说明原因 + 一键"重新获取本章"（只重建本章图片缓存）
            if (staleReason.isNotBlank() && !loading) {
                Surface(
                    color = WnColors.surface,
                    border = BorderStroke(1.dp, WnColors.accent),
                    modifier = Modifier.fillMaxWidth().align(Alignment.TopCenter)
                        .testTag("stale_cache_banner"),
                ) {
                    Row(
                        Modifier.fillMaxWidth().padding(horizontal = WnSpace.md, vertical = WnSpace.sm),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(
                            staleReason,
                            color = WnColors.ink,
                            style = MaterialTheme.typography.labelSmall,
                            modifier = Modifier.weight(1f),
                        )
                        TextButton(
                            enabled = !rebuilding,
                            modifier = Modifier.testTag("stale_cache_rebuild"),
                            onClick = {
                                scope.launch {
                                    rebuilding = true
                                    val cur = d?.readingChapters?.getOrNull(chapterIndex)?.id.orEmpty()
                                    val r = gateway.httpPost(
                                        ep.port,
                                        mangaPath(d!!.source, d.comicId,
                                            "/rebuild-images?chapter=${Uri.encode(cur)}"),
                                    )
                                    cache.remove(cur)
                                    rebuilding = false
                                    if (!r.ok) {
                                        error = "重新获取失败：HTTP ${r.code} " + r.body.take(120)
                                    } else {
                                        loadChapter(chapterIndex)
                                    }
                                }
                            },
                        ) { Text(if (rebuilding) "获取中…" else "重新获取") }
                    }
                }
            }

            if (showToc && d != null) {
                Surface(Modifier.fillMaxSize(), color = bg) {
                    Column(Modifier.fillMaxSize()) {
                        Row(Modifier.fillMaxWidth().padding(WnSpace.sm), verticalAlignment = Alignment.CenterVertically) {
                            TextButton(onClick = {
                                showToc = false
                            }) { Text("关闭") }
                            Text("目录（${d.readingChapters.size} 个单元）", color = WnColors.ink,
                                 style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                        }
                        HorizontalDivider(color = WnColors.line)
                        // 重开目录时直接回到当前阅读单元附近，长目录无需反复从卷首找进度。
                        // 每个章节占一个 LazyColumn item（卷标题包含在对应章节 item 内），
                        // 所以章节索引可直接作为初始列表位置。
                        val tocListState = rememberLazyListState(
                            initialFirstVisibleItemIndex = chapterIndex.coerceIn(
                                0, d.readingChapters.lastIndex.coerceAtLeast(0)),
                        )
                        LaunchedEffect(chapterIndex, d.readingChapters.size) {
                            if (d.readingChapters.isNotEmpty()) {
                                tocListState.scrollToItem(
                                    chapterIndex.coerceIn(0, d.readingChapters.lastIndex),
                                )
                            }
                        }
                        BoxWithConstraints(Modifier.weight(1f).fillMaxWidth()) {
                            LazyColumn(
                                Modifier.fillMaxSize().testTag("manga_toc"),
                                state = tocListState,
                            ) {
                                itemsIndexed(d.readingChapters) { i, c ->
                                    if (c.group.isNotBlank() &&
                                        (i == 0 || d.readingChapters[i - 1].group != c.group)) {
                                        Text(c.group, color = WnColors.ink,
                                             style = MaterialTheme.typography.titleSmall,
                                             fontWeight = FontWeight.Bold,
                                             modifier = Modifier.padding(start = WnSpace.lg, top = WnSpace.md))
                                    }
                                    Row(
                                        Modifier.fillMaxWidth().clickable {
                                            showToc = false
                                            controlsVisible = false
                                            userNavigated = true
                                            scope.launch {
                                                saveProgress(chapterIndex, pageNo)
                                                loadChapter(i)
                                            }
                                        }.padding(horizontal = WnSpace.lg, vertical = WnSpace.md)
                                            .then(if (i == chapterIndex)
                                                Modifier.testTag("manga_toc_current_chapter") else Modifier),
                                        verticalAlignment = Alignment.CenterVertically,
                                    ) {
                                        Text(
                                            c.label,
                                            color = if (i == chapterIndex) MaterialTheme.colorScheme.primary else WnColors.ink,
                                            style = MaterialTheme.typography.bodyMedium,
                                            maxLines = 1, overflow = TextOverflow.Ellipsis,
                                            modifier = Modifier.weight(1f),
                                        )
                                        if (d.downloaded.contains(c.id)) {
                                            Text("已下载", color = WnColors.inkDim,
                                                 style = MaterialTheme.typography.labelSmall)
                                        }
                                    }
                                    HorizontalDivider(color = WnColors.line)
                                }
                                // Give even the final chapter enough scroll room to align near
                                // the top, instead of being clamped to the viewport bottom.
                                item(key = "manga_toc_tail_space") {
                                    Spacer(Modifier.height(maxHeight))
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
