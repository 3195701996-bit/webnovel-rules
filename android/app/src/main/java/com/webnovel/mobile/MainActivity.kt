@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)

package com.webnovel.mobile

import android.annotation.SuppressLint
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.util.Log
import android.webkit.CookieManager
import android.webkit.SslErrorHandler
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.MenuBook
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.ErrorOutline
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.saveable.rememberSaveableStateHolder
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import coil.ImageLoader
import coil.compose.AsyncImage
import coil.request.ImageRequest
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlin.coroutines.resume
import okhttp3.OkHttpClient
import java.util.concurrent.TimeUnit

/**
 * 阅读器主界面（原生 Compose）。
 *
 * 设计依据（《手机独立服务器 APK 规划》与《阶段 C 诊断与阅读器改造建议》）：
 * - 首页是**书架**，不是调试控制台；用户不需要点"启动服务"。
 * - 导航：书架 / 浏览 / 下载 / 设置；详情、阅读、浏览、搜索全部原生
 *   WebView，仅回环），原生详情/阅读页后续替换。
 * - 引擎启停/端口/认证/重连/超时全部由 EngineGateway 承担，界面只渲染
 *   loading / ready / error+重试（不再黑屏、不再打印运行对象）。
 */
class MainActivity : ComponentActivity() {

    companion object { private const val TAG = "MobileServerUI" }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val gateway = EngineGateway(applicationContext)
        // 前台服务先行：引擎不依赖界面是否成功渲染
        startEngineService()
        setContent { MaterialTheme(colorScheme = wnColorScheme()) { ReaderApp(gateway) } }
    }

    private fun startEngineService() {
        val svc = android.content.Intent(this, LocalServerService::class.java)
            .setAction(LocalServerService.ACTION_START)
        try {
            if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O)
                startForegroundService(svc) else startService(svc)
        } catch (t: Throwable) {
            Log.w(TAG, "前台服务启动失败（引擎仍可由绑定启动）: ${t.javaClass.simpleName}")
        }
    }
}

// ── 顶层：引擎状态机 ─────────────────────────────────────────

@Composable
fun ReaderApp(gateway: EngineGateway) {
    var engine by remember { mutableStateOf<EngineState>(EngineState.Starting) }
    var stage by remember { mutableStateOf("正在启动本机引擎") }
    val scope = rememberCoroutineScope()

    fun reconnect() {
        engine = EngineState.Starting
        stage = "正在重新连接引擎"
        scope.launch { engine = gateway.connect { stage = it } }
    }

    LaunchedEffect(Unit) {
        engine = try {
            gateway.connect { stage = it }
        } catch (c: kotlinx.coroutines.CancellationException) { engine }
    }

    // 方向基线 §5.4：主框架**不再**只在 ready 时出现——否则错误页承诺的
    // "已下载内容仍可离线阅读"根本进不去。现在始终渲染外壳，未就绪时给横幅，
    // 书架退化为本地只读索引（离线可读已下载内容）。
    AppShell(gateway, engine, stage, onRetry = { reconnect() })
}

/**
 * 应用外壳：把"引擎状态"当成一个**可降级**的输入，而不是"能不能进 App"的开关。
 * 拆成独立 composable 也便于验收用例直接以 Failed 状态渲染（不需要真的把引擎弄挂）。
 */
@Composable
internal fun AppShell(
    gateway: EngineGateway,
    engine: EngineState,
    stage: String,
    onRetry: () -> Unit,
) {
    val failed = engine as? EngineState.Failed
    val ready = engine as? EngineState.Ready
    var showDiag by remember { mutableStateOf(false) }

    if (failed != null && showDiag) {
        // 诊断页不依赖引擎：直接展示本地信息，返回回到外壳
        EngineError(failed, onRetry = { showDiag = false; onRetry() },
            onClose = { showDiag = false })
        return
    }
    // 启动中（Idle/Starting）如实显示"正在启动"，**不能**复用失败态的界面：
    // 正常启动时挂一条"本机引擎未就绪 + 重试"会把用户吓一跳，也让"未就绪"
    // 这句话失去意义（实测把冷启动验收用例都带崩了：它断言启动成功后不该出现该文案）。
    // 只有真的 Failed 才降级到"横幅 + 离线书架"。
    if (failed == null && ready == null) {
        EngineLoading(stage)
        return
    }
    HomeScaffold(
        gateway = gateway,
        ep = (engine as? EngineState.Ready)?.endpoint,
        engineStage = if (failed != null) failed.stage else stage,
        engineDetail = failed?.detail ?: "",
        onRetry = onRetry,
        onDiag = { showDiag = true },
    )
}

@Composable
private fun EngineLoading(stage: String) = Surface(Modifier.fillMaxSize()) {
    Column(Modifier.fillMaxSize(), Arrangement.Center, Alignment.CenterHorizontally) {
        Icon(
            Icons.AutoMirrored.Filled.MenuBook,
            contentDescription = null,
            tint = WnColors.inkDim,
            modifier = Modifier.size(56.dp),
        )
        Spacer(Modifier.height(WnSpace.lg))
        CircularProgressIndicator()
        Spacer(Modifier.height(WnSpace.lg))
        Text(stage, style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurface)
        Spacer(Modifier.height(WnSpace.xs))
        Text("首次启动需要解包内置引擎，可能需要十几秒",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

/**
 * 引擎未就绪：**这里必须能自诊断**。
 *
 * 过去文案写"若持续失败，请在设置 → 诊断查看详情"——可引擎没起来时主框架
 * 根本不显示，设置页进不去，等于指了一个到不了的地方（方向基线 §5.4）。
 * 现在把诊断信息直接摊在这一页：阶段、原始错误、本机引擎自检要点。
 */
@Composable
private fun EngineError(st: EngineState.Failed, onRetry: () -> Unit,
                        onClose: (() -> Unit)? = null) {
    var showDiag by remember { mutableStateOf(onClose == null) }
    Surface(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize().padding(WnSpace.xl)
                   .verticalScroll(rememberScrollState())) {
            Spacer(Modifier.height(WnSpace.xxl))
            Icon(
                Icons.Filled.ErrorOutline,
                contentDescription = null,
                tint = WnColors.danger,
                modifier = Modifier.size(48.dp),
            )
            Spacer(Modifier.height(WnSpace.md))
            Text("本机引擎未就绪", style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.error)
            Spacer(Modifier.height(WnSpace.sm))
            Text("阶段：${st.stage}", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurface)
            Text(st.detail, style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(WnSpace.lg))
            Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                Button(onClick = onRetry) { Text("重试") }
                if (onClose != null) {
                    OutlinedButton(onClick = onClose,
                        modifier = Modifier.testTag("engine_error_close")) { Text("返回书架") }
                } else {
                    OutlinedButton(onClick = { showDiag = !showDiag },
                        modifier = Modifier.testTag("engine_error_diag")) {
                        Text(if (showDiag) "收起诊断" else "诊断信息")
                    }
                }
            }
            if (showDiag) {
                Spacer(Modifier.height(WnSpace.md))
                Text(EngineDiagnostics.text(st), style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(WnSpace.md))
            Text("下载/已下载内容与书源配置都在应用私有目录里，重试不会清数据；" +
                "首启解包通常需要十几秒，请先重试一次。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

// ── 主框架：四个页签 + 阅读器页面 ────────────────────────────

/** 导航目的地：核心流程全部原生（详情/搜索/阅读/浏览/下载）。
 *
 *  Web 只保留给"高级诊断"，不是任何核心流程的落点——空书架、搜索、
 *  详情、阅读、浏览一律走原生目的地（方向基线 §6.3）。 */
internal sealed interface Dest {
    data class Detail(val key: String) : Dest
    data class Novel(val key: String, val index: Int, val pct: Int) : Dest
    data class MangaDetail(val source: String, val comicId: String) : Dest
    data class Manga(val source: String, val comicId: String, val index: Int,
                     val page: Int,
                     /**
                      * 落点是否**精确**（服务端按章节身份解析的结果，0.64.0）。
                      * false = 目录变了、只能近似定位：阅读器此时**不得**用自动
                      * 保存覆盖用户的阅读记录，否则"上次读到第47话"会被写成第1话。
                      */
                     val trusted: Boolean = true,
                     /** 服务端给出的定位说明（目录变化时向用户解释） */
                     val note: String = "",
                     /**
                      * 上次读到的那一话的**身份**（章节 id + 章名）。阅读器会拿它
                      * 在自己取到的章节列表里再定位一次——两份列表可能不同（详情页
                      * 走下载缓存、阅读器走最新详情），只传下标会指到别的一话。
                      */
                     val startChapterId: String = "",
                     val startLabel: String = "") : Dest
    data object Explore : Dest
    data object MangaSources : Dest
    data class MangaSource(val key: String, val name: String) : Dest
    data object ReaderPrefs : Dest
    data object History : Dest
    data object NovelSearch : Dest
    /** 漫画搜索：可带预置源/排序（从单源页"在此源搜索"进来时用） */
    data class MangaSearch(val sourceKey: String = "", val order: String = "") : Dest
    data class OfflineNovel(val key: String, val index: Int) : Dest
    data class OfflineManga(val source: String, val comicId: String) : Dest
    data object Sources : Dest
    data object Storage : Dest
    data object Backup : Dest
    data class Web(val path: String, val title: String) : Dest
}

/**
 * 目的地 → 稳定的状态 key（P0-E）。
 *
 * 同一类页面共用一个 key，返回时才能恢复；不同条目（不同书/不同源）分开，
 * 避免 A 书的状态被 B 书复用。核心流程的 key 只取"页面身份"，不含页码/查询词——
 * 那些本身就是要被恢复的状态。
 */
internal fun destKey(d: Dest): String = when (d) {
    is Dest.Detail -> "Detail:${d.key}"
    is Dest.Novel -> "Novel:${d.key}"
    is Dest.MangaDetail -> "MangaDetail:${d.source}:${d.comicId}"
    is Dest.Manga -> "Manga:${d.source}:${d.comicId}"
    Dest.Explore -> "Explore"
    Dest.MangaSources -> "MangaSources"
    is Dest.MangaSource -> "MangaSource:${d.key}"
    Dest.ReaderPrefs -> "ReaderPrefs"
    Dest.History -> "History"
    Dest.NovelSearch -> "NovelSearch"
    is Dest.MangaSearch -> "MangaSearch:${d.sourceKey}:${d.order}"
    is Dest.OfflineNovel -> "OfflineNovel:${d.key}"
    is Dest.OfflineManga -> "OfflineManga:${d.source}:${d.comicId}"
    Dest.Sources -> "Sources"
    Dest.Storage -> "Storage"
    Dest.Backup -> "Backup"
    is Dest.Web -> "Web:${d.path}"
}

@Composable
private fun HomeScaffold(
    gateway: EngineGateway,
    ep: EngineEndpoint?,
    engineStage: String = "",
    engineDetail: String = "",
    onRetry: () -> Unit = {},
    onDiag: () -> Unit = {},
) {
    var tab by remember { mutableIntStateOf(0) }
    val stack = remember { mutableStateListOf<Dest>() }
    val context = LocalContext.current
    // 离线模式也要有 loader（读本地图片文件，不需要引擎凭据）
    val loader = remember(ep) {
        engineImageLoader(context, ep)
    }

    // 代理功能已下线（用户反馈：换包装新版本后，历史配置的代理会把全部源拖死，
    // 每次都得清一次应用数据才能用）。引擎就绪时自查一次：只要还存着已保存的
    // 代理（env 环境变量不是这里存的，不动），就静默清除、恢复直连 ——
    // 老用户升级后无需再手动清数据。
    LaunchedEffect(ep) {
        if (ep != null) {
            runCatching {
                val r = gateway.httpText(ep.port, "/api/net/proxy")
                val d = runCatching {
                    org.json.JSONObject(r.body).optJSONObject("data")
                }.getOrNull()
                val proxy = d?.optString("proxy").orEmpty()
                val src = d?.optString("source").orEmpty()
                if (r.ok && proxy.isNotBlank() && src != "env") {
                    gateway.httpPost(ep.port, "/api/net/proxy",
                        org.json.JSONObject().put("proxy", "").toString())
                }
            }
        }
    }
    val push: (Dest) -> Unit = { d -> stack.add(d) }
    val pop: () -> Unit = { if (stack.isNotEmpty()) stack.removeAt(stack.lastIndex) }

    // 覆盖层：全部原生页面（网页只作为设置里的诊断入口）。返回手势与返回键都按栈回退。
    //
    // P0-E：每个目的地用**稳定的 key** 包一层 SaveableStateHolder。
    // 只渲染栈顶会让下层页面退出组合、`remember` 状态全丢——这正是"从详情返回后
    // 分类结果/查询词/已加载页/滚动位置都没了"的根因。SaveableStateHolder 会在页面
    // 离开组合时保存其 rememberSaveable 状态（含 rememberLazyListState 的滚动位置），
    // 同 key 再进入时原样恢复。
    val stateHolder = rememberSaveableStateHolder()
    if (stack.isNotEmpty()) {
        BackHandler { pop() }
        stateHolder.SaveableStateProvider(destKey(stack.last())) {
            val top = stack.last()
            val ready = ep
            when {
                // 离线页面不需要引擎：先于其它目的地处理
                top is Dest.OfflineNovel ->
                    OfflineNovelReaderScreen(top.key, top.index, onBack = pop)
                top is Dest.OfflineManga ->
                    OfflineMangaReaderScreen(top.source, top.comicId, loader, onBack = pop)
                // 阅读排版不依赖引擎：离线时也要能改（设置页在离线模式下只给它入口）
                top is Dest.ReaderPrefs -> ReaderPrefsScreen(onBack = pop)
                ready == null -> {
                    // 引擎不可用时不该到得了其它页面（入口都在引擎可用分支）。
                    // 真到了就如实说明，而不是崩溃或白屏。
                    Column(Modifier.fillMaxSize().padding(24.dp), Arrangement.Center) {
                        Text("这个页面需要本机引擎", style = MaterialTheme.typography.titleSmall)
                        Spacer(Modifier.height(6.dp))
                        Text("先返回并点「重试」；离线时可以在「书架」里读已下载的内容。",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.outline)
                        Spacer(Modifier.height(8.dp))
                        Button(onClick = pop) { Text("返回") }
                    }
                }
                else -> when (top) {
                    is Dest.Detail -> BookDetailScreen(gateway, ready, top.key, onBack = pop,
                        onRead = { idx -> push(Dest.Novel(top.key, idx, 0)) })
                    is Dest.Novel -> NovelReaderScreen(gateway, ready, top.key, top.index,
                        top.pct, onBack = pop)
                    is Dest.MangaDetail -> MangaDetailScreen(gateway, ready, top.source,
                        top.comicId, loader, onBack = pop,
                        onRead = { idx, page, exact, note, chId, chLabel ->
                            push(Dest.Manga(top.source, top.comicId, idx, page,
                                            trusted = exact, note = note,
                                            startChapterId = chId, startLabel = chLabel))
                        })
                    is Dest.Manga -> MangaReaderScreen(gateway, ready, top.source, top.comicId,
                        top.index, top.page, loader, onBack = pop,
                        startTrusted = top.trusted, resumeNote = top.note,
                        startChapterId = top.startChapterId, startLabel = top.startLabel)
                    is Dest.Explore -> ExploreScreen(gateway, ready, loader, push, onBack = pop)
                    is Dest.MangaSources -> MangaSourceListScreen(gateway, ready, push, onBack = pop)
                    is Dest.MangaSource -> MangaSourceScreen(gateway, ready, loader, top.key,
                        top.name, push, onBack = pop)
                    is Dest.History -> HistoryScreen(gateway, ready, push, onBack = pop)
                    is Dest.NovelSearch -> NovelSearchScreen(gateway, ready, push, onBack = pop)
                    is Dest.MangaSearch -> MangaSearchScreen(gateway, ready, loader, push,
                        onBack = pop,
                        // 从单源页"在此源搜索"进来时带上源/排序；"全部源"进来时为空
                        presetSource = top.sourceKey, presetOrder = top.order)
                    is Dest.Sources -> BookSourceScreen(gateway, ready, onBack = pop)
                    Dest.Storage -> StorageScreen(gateway, ready, onBack = pop)
                    is Dest.Backup -> BackupScreen(gateway, ready,
                        appVersion = BuildConfig.VERSION_NAME, onBack = pop)
                    is Dest.Web -> ReaderWebView(gateway, ready, top.path, top.title, onBack = pop)
                    is Dest.OfflineNovel, is Dest.OfflineManga, is Dest.ReaderPrefs -> Unit
                }
            }
        }
        return
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Text(when (tab) {
                        0 -> "书架"; 1 -> "浏览"; 2 -> "下载"; else -> "设置"
                    })
                },
                // 「最近阅读」已并入书架作为独立页签（书架内双页：最近阅读/已缓存），
                // 顶栏不再单设历史入口。Dest.History 保留供兼容性跳转。
            )
        },
        bottomBar = {
            NavigationBar {
                val tabs = listOf(
                    Triple(Icons.AutoMirrored.Filled.MenuBook, "书架", 0),
                    Triple(Icons.Filled.Search, "浏览", 1),
                    Triple(Icons.Filled.Download, "下载", 2),
                    Triple(Icons.Filled.Settings, "设置", 3),
                )
                tabs.forEach { (icon, label, i) ->
                    NavigationBarItem(
                        selected = tab == i,
                        onClick = { tab = i },
                        icon = { Icon(icon, contentDescription = label) },
                        label = { Text(label) },
                        colors = NavigationBarItemDefaults.colors(
                            indicatorColor = MaterialTheme.colorScheme.primary,
                            selectedIconColor = MaterialTheme.colorScheme.onPrimary,
                            selectedTextColor = MaterialTheme.colorScheme.primary,
                        ),
                    )
                }
            }
        },
    ) { pad ->
        Box(Modifier.padding(pad).fillMaxSize()) {
            Column(Modifier.fillMaxSize()) {
                if (ep == null) {
                    EngineBanner(engineStage, engineDetail, onRetry, onDiag)
                }
                Box(Modifier.weight(1f)) {
                    when (tab) {
                        0 -> if (ep == null) {
                            // 引擎不可用 → 本地只读索引（已下载的小说/漫画）
                            OfflineShelfContent(
                                data = rememberOfflineShelf(),
                                onOpenNovel = { key, idx -> stack.add(Dest.OfflineNovel(key, idx)) },
                                onOpenManga = { src, cid ->
                                    stack.add(Dest.OfflineManga(src, cid))
                                },
                            )
                        } else {
                            ShelfScreen(gateway, ep, loader, push)
                        }
                        1 -> if (ep == null) {
                            Column(Modifier.fillMaxSize().padding(24.dp), Arrangement.Center) {
                                Text("浏览与搜索需要本机引擎",
                                    style = MaterialTheme.typography.titleSmall)
                                Spacer(Modifier.height(6.dp))
                                Text("先点上方「重试」；离线时可以在「书架」里读已下载的内容。",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.outline)
                            }
                        } else {
                            BrowseScreen(gateway, ep, push)
                        }
                        2 -> if (ep == null) {
                            Column(Modifier.fillMaxSize().padding(24.dp), Arrangement.Center) {
                                Text("下载队列需要本机引擎",
                                    style = MaterialTheme.typography.titleSmall)
                                Spacer(Modifier.height(6.dp))
                                Text("已下载的内容仍可在「书架」离线阅读。",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.outline)
                            }
                        } else {
                            DownloadsScreen(gateway, ep, push)
                        }
                        else -> if (ep == null) {
                            // 离线设置：只显示**不依赖引擎**的部分（阅读设置、产物基线、
                            // 本地诊断），其余入口如实说明需要引擎
                            OfflineSettingsScreen(
                                onOpenReaderPrefs = { stack.add(Dest.ReaderPrefs) })
                        } else {
                            SettingsScreen(gateway, ep, push)
                        }
                    }
                }
            }
        }
    }
}

/**
 * 引擎图片加载器：图片来自**本机回环的引擎**（封面、漫画页），会话凭据经请求头注入
 * （token 不进 URL）。
 *
 * **必须关掉 Coil 的联网状态观察**（`networkObserverEnabled(false)`）：
 * Coil 2.x 默认会在系统报告"没有网络"时**直接让 http 请求失败**，而我们的图片
 * 来源是本机回环——已下载的漫画页就在本机文件里，跟外网没有任何关系。
 * 设备实测（飞行模式）：不关它，打开已下载的漫画会整页显示"页加载失败"，
 * 正是方向基线 §8.D"完成后断网重启，已下载内容可打开"要挡住的问题。
 * 这是"真断网"用例抓出来的真实缺陷，不是测试环境问题。
 */
// ── 图片缓存（可调上限 + 可清除；手机端不限制缓存占用，上限由用户定）──
private const val CACHE_PREFS = "cache_prefs"
private const val KEY_IMG_CACHE_LIMIT_MB = "img_cache_limit_mb"
internal const val DEFAULT_IMG_CACHE_LIMIT_MB = 512

/** 当前图片缓存上限（MB；0 = 不限） */
internal fun imageCacheLimitMb(context: android.content.Context): Int =
    context.getSharedPreferences(CACHE_PREFS, android.content.Context.MODE_PRIVATE)
        .getInt(KEY_IMG_CACHE_LIMIT_MB, DEFAULT_IMG_CACHE_LIMIT_MB)

/**
 * 设置缓存上限（MB，0=不限）。**下次启动生效**：运行期不更换磁盘缓存实例，
 * 避免正在使用的加载器写入已关闭的缓存（界面会如实说明这一点）。
 */
internal fun setImageCacheLimitMb(context: android.content.Context, mb: Int) {
    context.getSharedPreferences(CACHE_PREFS, android.content.Context.MODE_PRIVATE)
        .edit().putInt(KEY_IMG_CACHE_LIMIT_MB, mb).apply()
}

private fun imageCacheLimitBytes(context: android.content.Context): Long {
    val mb = imageCacheLimitMb(context)
    return if (mb <= 0) 32L * 1024 * 1024 * 1024 else mb.toLong() * 1024 * 1024
}

/** 双通道共享的图片磁盘缓存：预取通道填、交互通道读，跨版本留存 */
private val imageDiskCacheLock = Any()
@Volatile private var sharedImageDiskCache: coil.disk.DiskCache? = null
private fun imageDiskCache(context: android.content.Context): coil.disk.DiskCache =
    sharedImageDiskCache ?: synchronized(imageDiskCacheLock) {
        sharedImageDiskCache ?: coil.disk.DiskCache.Builder()
            .directory(context.cacheDir.resolve("img_shared"))
            .maxSizeBytes(imageCacheLimitBytes(context))
            .build().also { sharedImageDiskCache = it }
    }

private val activeImageLoaders = java.util.concurrent.CopyOnWriteArrayList<ImageLoader>()

/** 当前图片缓存占用（字节；调用方自行切到 IO 线程） */
internal fun imageCacheSizeBytes(context: android.content.Context): Long {
    val dir = context.cacheDir.resolve("img_shared")
    return runCatching {
        dir.walkTopDown().filter { it.isFile }.sumOf { it.length() }
    }.getOrDefault(0L)
}

/** 清除全部图片缓存（磁盘 + 各通道内存缓存），立即生效 */
@OptIn(coil.annotation.ExperimentalCoilApi::class)
internal fun clearImageCache(context: android.content.Context) {
    runCatching { imageDiskCache(context).clear() }
    activeImageLoaders.forEach { it.memoryCache?.clear() }
}

internal fun engineImageLoader(context: android.content.Context,
                              ep: EngineEndpoint?,
                              maxPerHost: Int = 6,
                              smallMemoryCache: Boolean = false): ImageLoader =
    ImageLoader.Builder(context)
        .networkObserverEnabled(false)
        .diskCache { imageDiskCache(context) }
        // 双通道调速（引擎 waitress 16 线程）：交互通道 6 并发（可见页/封面，多为
        // 缓存命中、毫秒级返回），预取通道 3 并发（后台慢速跟进）。图片合计最多
        // 占 9 个线程，永远给 API（搜索/进度保存/切章）留足线程——并发上限按
        // 源站耐受度封顶，不盲目拉满（16 并发触发风控的教训）。
        .apply {
            if (smallMemoryCache) {
                memoryCache {
                    coil.memory.MemoryCache.Builder(context)
                        .maxSizeBytes(8 * 1024 * 1024).build()
                }
            }
        }
        .okHttpClient {
            OkHttpClient.Builder()
                .dispatcher(okhttp3.Dispatcher().apply {
                    maxRequestsPerHost = maxPerHost
                })
                .addInterceptor { chain ->
                    val req = chain.request().newBuilder()
                        .apply { if (ep != null && ep.token.isNotEmpty()) header("X-Mobile-Token", ep.token) }
                        .build()
                    chain.proceed(req)
                }
                .connectTimeout(8, TimeUnit.SECONDS)
                .readTimeout(20, TimeUnit.SECONDS)
                .build()
        }
        .crossfade(true)
        .build()
        .also { activeImageLoaders.add(it) }

// ── 书架 ─────────────────────────────────────────────────────

/** 阅读历史条目：小说与漫画合并后按 lastReadTs 降序排列 */
private sealed interface ShelfHistoryEntry {
    val ts: Double
    data class NovelEntry(val item: NovelItem) : ShelfHistoryEntry {
        override val ts: Double get() = item.lastReadTs
    }
    /** 漫画历史条目（/api/manga/history）：含在线读过但未下载的作品 */
    data class MangaHistEntry(val item: EngineData.MangaHistory) : ShelfHistoryEntry {
        override val ts: Double get() = item.ts
    }
}

// 排序偏好持久化（镜像桌面端 library.html 的 lib_book_sort/lib_manga_sort 语义）
private const val SHELF_PREFS = "shelf_prefs"
private const val SORT_DOWNLOADED = "downloaded"
private const val SORT_READ = "read"

private fun readShelfSort(sp: android.content.SharedPreferences, key: String): String {
    val v = sp.getString(key, SORT_DOWNLOADED) ?: SORT_DOWNLOADED
    return if (v == SORT_READ) SORT_READ else SORT_DOWNLOADED
}

@Composable
private fun ShelfScreen(gateway: EngineGateway, ep: EngineEndpoint, loader: ImageLoader,
                        onOpen: (Dest) -> Unit) {
    var novels by remember { mutableStateOf<List<NovelItem>>(emptyList()) }
    var manga by remember { mutableStateOf<List<MangaItem>>(emptyList()) }
    // 漫画**全量**阅读历史（/api/manga/history，含在线读过但未下载的条目）
    var mangaHist by remember {
        mutableStateOf<List<EngineData.MangaHistory>>(emptyList())
    }
    // 有新话标记：取自最近一次「检查书库更新」的结果（服务端会保留批次结果）。
    // 没检查过就没有标记——不编造"有更新"。
    var mangaUpdates by remember { mutableStateOf<Map<String, String>>(emptyMap()) }
    var loading by remember { mutableStateOf(true) }
    var refreshing by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    val shelfPrefs = remember {
        context.getSharedPreferences(SHELF_PREFS, android.content.Context.MODE_PRIVATE)
    }
    var bookSort by remember { mutableStateOf(readShelfSort(shelfPrefs, "book_sort")) }
    var mangaSort by remember { mutableStateOf(readShelfSort(shelfPrefs, "manga_sort")) }

    fun refresh() {
        scope.launch {
            if (!loading) refreshing = true
            error = null
            try {
                // 四个读接口互相独立：并行发出（回环本机引擎，省 3 次串行往返）
                val nbD = async { gateway.httpText(ep.port, "/api/books") }
                val mbD = async { gateway.httpText(ep.port, "/api/manga/library") }
                val hrD = async { gateway.httpText(ep.port, "/api/manga/history") }
                val upD = async {
                    gateway.httpText(ep.port, "/api/manga/library/check-updates/status")
                }
                val nb = nbD.await()
                val mb = mbD.await()
                val hr = hrD.await()
                val up = upD.await()
                // 漫画全量阅读历史（含在线读过但未下载的）——最近阅读页的数据源
                if (hr.ok) mangaHist = EngineData.mangaHistory(hr.body)
                if (up.ok) {
                    val lib = EngineData.mangaLibraryUpdate(up.body)
                    mangaUpdates = lib.withUpdate.associate { (title, c) -> title to c.label }
                }
                novels = EngineData.novels(nb.body)
                manga = EngineData.manga(mb.body)
                if (!nb.ok && !mb.ok) error = "读取书架失败：HTTP ${nb.code}/${mb.code}"
            } catch (t: Throwable) {
                error = "${t.javaClass.simpleName}: ${t.message}"
            }
            loading = false
            refreshing = false
        }
    }
    LaunchedEffect(ep) { refresh() }

    // 最近阅读：started 的小说 + 漫画**全量阅读历史**（含在线读过但未下载的），
    // 合并按时间降序，不限条数
    val history = remember(novels, mangaHist) {
        (novels.filter { it.started }.map { ShelfHistoryEntry.NovelEntry(it) } +
            mangaHist.map { ShelfHistoryEntry.MangaHistEntry(it) })
            .sortedByDescending { it.ts }
    }
    // 已缓存排序：最近下载 = updated_at/downloaded_at 字典序降序（空串垫底）；
    // 最近阅读 = lastReadTs 降序（0 垫底）
    val sortedNovels = remember(novels, bookSort) {
        if (bookSort == SORT_READ) novels.sortedByDescending { it.lastReadTs }
        else novels.sortedByDescending { it.updatedAt }
    }
    val sortedManga = remember(manga, mangaSort) {
        if (mangaSort == SORT_READ) manga.sortedByDescending { it.lastReadTs }
        else manga.sortedByDescending { it.downloadedAt }
    }

    PullToRefreshBox(
        isRefreshing = refreshing,
        onRefresh = { refresh() },
        modifier = Modifier.fillMaxSize(),
    ) {
        when {
            loading -> Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
            error != null -> Column(Modifier.fillMaxSize().padding(WnSpace.xl), Arrangement.Center) {
                Text(error!!, color = MaterialTheme.colorScheme.error)
                Spacer(Modifier.height(WnSpace.md))
                Button(onClick = { refresh() }) { Text("重试") }
            }
            novels.isEmpty() && manga.isEmpty() -> EmptyShelf(
                onSearchManga = { onOpen(Dest.MangaSearch()) },
                onBrowseMangaSources = { onOpen(Dest.MangaSources) },
                onSearchNovel = { onOpen(Dest.NovelSearch) },
            )
            else -> {
                // 书架双页：「最近阅读」在前、「已缓存」在后，两个平行且独立的页面；
                // 分段页签 + 左右滑动切换，页位随状态保存恢复。
                var page by rememberSaveable { mutableIntStateOf(0) }
                val pagerState = rememberPagerState(initialPage = page, pageCount = { 2 })
                LaunchedEffect(pagerState.currentPage) { page = pagerState.currentPage }
                Column(Modifier.fillMaxSize()) {
                    WnSegmentedTabs(
                        options = listOf("最近阅读", "已缓存"),
                        selected = pagerState.currentPage,
                        onSelect = { scope.launch { pagerState.animateScrollToPage(it) } },
                        modifier = Modifier.padding(horizontal = WnSpace.md,
                            vertical = WnSpace.sm),
                    )
                    HorizontalPager(state = pagerState,
                                    modifier = Modifier.fillMaxSize()) { p ->
                        if (p == 0) {
                            // ── 页一：最近阅读 ──
                            // 所有读过的小说+漫画（不限条数），lastReadTs 降序；
                            // 小说无封面数据源 → 灰底「暂无封面」占位，漫画用真实封面。
                            LazyColumn(Modifier.fillMaxSize(),
                                       contentPadding = PaddingValues(WnSpace.md),
                                       verticalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                                if (history.isEmpty()) {
                                    item {
                                        Text("还没有阅读记录",
                                            style = MaterialTheme.typography.bodySmall,
                                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                                            modifier = Modifier.padding(WnSpace.xl))
                                    }
                                } else {
                                    items(history) { entry ->
                                        when (entry) {
                                            is ShelfHistoryEntry.NovelEntry -> {
                                                val n = entry.item
                                                ShelfNovelRow(n) {
                                                    onOpen(Dest.Detail(n.key))
                                                }
                                            }
                                            is ShelfHistoryEntry.MangaHistEntry -> {
                                                val h = entry.item
                                                ShelfHistMangaRow(h, ep, loader) {
                                                    onOpen(Dest.MangaDetail(h.source, h.comicId))
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        } else {
                            // ── 页二：已缓存（小说/漫画两个子区块，各自带排序）──
                            LazyColumn(Modifier.fillMaxSize(),
                                       contentPadding = PaddingValues(WnSpace.md),
                                       verticalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                                if (sortedNovels.isNotEmpty()) {
                                    item {
                                        WnSectionHeader("小说", count = sortedNovels.size,
                                            trailing = {
                                                WnSortMenu(bookSort) { v ->
                                                    bookSort = v
                                                    shelfPrefs.edit()
                                                        .putString("book_sort", v).apply()
                                                }
                                            })
                                    }
                                    items(sortedNovels) { n ->
                                        ShelfNovelRow(n) { onOpen(Dest.Detail(n.key)) }
                                    }
                                }
                                if (sortedManga.isNotEmpty()) {
                                    item {
                                        WnSectionHeader("漫画", count = sortedManga.size,
                                            trailing = {
                                                WnSortMenu(mangaSort) { v ->
                                                    mangaSort = v
                                                    shelfPrefs.edit()
                                                        .putString("manga_sort", v).apply()
                                                }
                                            })
                                    }
                                    item {
                                        LazyVerticalGrid(
                                            columns = GridCells.Fixed(3),
                                            modifier = Modifier.height(
                                                ((sortedManga.size + 2) / 3 * 172).dp),
                                            horizontalArrangement =
                                                Arrangement.spacedBy(WnSpace.sm),
                                            verticalArrangement =
                                                Arrangement.spacedBy(WnSpace.sm),
                                        ) {
                                            items(sortedManga.size) { i ->
                                                val m = sortedManga[i]
                                                MangaCard(m, ep, loader, mangaUpdates[m.title]) {
                                                    onOpen(Dest.MangaDetail(m.source, m.comicId))
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

/** 最近阅读的漫画行：封面（本地优先接口；在线读过的也有阅读缓存封面）+ 标题 + 进度 + 时间 */
@Composable
private fun ShelfHistMangaRow(h: EngineData.MangaHistory, ep: EngineEndpoint,
                              loader: ImageLoader, onClick: () -> Unit) {
    WnHairlineCard(onClick = onClick) {
        Row(
            Modifier.padding(WnSpace.md),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(WnSpace.md),
        ) {
            WnCoverImage(
                url = ep.imageOrigin + "/api/manga/${android.net.Uri.encode(h.source)}" +
                    "/${android.net.Uri.encode(h.comicId)}/cover",
                contentDescription = h.title,
                loader = loader,
                modifier = Modifier.width(56.dp).height(76.dp),
            )
            Column(Modifier.weight(1f)) {
                Text(h.title.ifBlank { h.comicId },
                    style = MaterialTheme.typography.bodyMedium, maxLines = 1,
                    overflow = TextOverflow.Ellipsis)
                Text(
                    (if (h.pos.isNotBlank()) "读到 ${h.pos}" else "第 ${h.idx + 1} 话") +
                        " · " + shelfTimeLabel(h.ts),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1,
                    overflow = TextOverflow.Ellipsis)
            }
        }
    }
}

/** 最近阅读时间（MM-dd HH:mm；ts<=0 如实说"时间未知"） */
private fun shelfTimeLabel(ts: Double): String {
    if (ts <= 0) return "时间未知"
    return try {
        java.text.SimpleDateFormat("MM-dd HH:mm", java.util.Locale.US)
            .format(java.util.Date((ts * 1000).toLong()))
    } catch (_: Throwable) {
        "时间未知"
    }
}

/**
 * 书架小说行（对齐 web 端书库卡片）：封面占位 + 状态徽标（完结/失败/下载中）+
 * 作者·章数 + 进度文案 + 阅读/下载双进度条。小说无封面数据源 → 灰底「暂无封面」。
 */
@Composable
private fun ShelfNovelRow(n: NovelItem, onClick: () -> Unit) {
    WnHairlineCard(onClick = onClick) {
        Row(
            Modifier.padding(WnSpace.md),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(WnSpace.md),
        ) {
            WnNoCover(Modifier.width(56.dp).height(76.dp))
            Column(Modifier.weight(1f),
                   verticalArrangement = Arrangement.spacedBy(WnSpace.xs)) {
                Row(verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                    Text(n.name, style = MaterialTheme.typography.bodyMedium,
                        fontWeight = FontWeight.SemiBold, maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f, fill = false))
                    // 状态徽标（与 web 端同口径）：完结 ≥100% / 有失败章 / 下载中
                    val (badge, badgeColor) = when {
                        n.percent >= 100 -> "完结" to WnColors.ok
                        n.failed > 0 -> "${n.failed} 章失败" to WnColors.danger
                        else -> "下载中" to WnColors.accent
                    }
                    Text(badge, style = MaterialTheme.typography.labelSmall,
                        color = badgeColor,
                        modifier = Modifier.clip(WnPillShape)
                            .border(1.dp, badgeColor, WnPillShape)
                            .padding(horizontal = 6.dp, vertical = 1.dp))
                }
                Text("${n.author.ifBlank { "佚名" }} · ${n.done}/${n.total} 章",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(n.progressLabel, style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1, overflow = TextOverflow.Ellipsis)
                // 双进度条（对齐 web 端）：阅读进度 accent、下载进度 ok
                if (n.readRatio > 0) ShelfBar(n.readRatio, WnColors.accent)
                ShelfBar(n.percent, WnColors.ok)
            }
        }
    }
}

/** 细进度条（书架行内用）：surfaceVar 轨道 + 彩色填充，3dp 高 */
@Composable
private fun ShelfBar(ratio: Int, color: Color, modifier: Modifier = Modifier) {
    Box(modifier.fillMaxWidth().height(3.dp).clip(WnChipShape)
            .background(WnColors.surfaceVar)) {
        Box(Modifier.fillMaxWidth(ratio.coerceIn(0, 100) / 100f).fillMaxSize()
                .clip(WnChipShape).background(color))
    }
}

/**
 * 空书架的首用入口：**全部原生**，且以漫画为默认内容类型（方向基线 §3.1 / P0-B）。
 *
 * 这里过去是单个"去搜索"按钮，直接打开桌面网页（`Dest.Web("/", "浏览与搜索")`）——
 * 用户截图里那个"原生外壳 + 桌面网页 + 回环地址失效"的首用界面就是它。
 * 内置源不需要导入，因此首用不需要任何网页步骤。
 */
@Composable
private fun EmptyShelf(onSearchManga: () -> Unit, onBrowseMangaSources: () -> Unit,
                       onSearchNovel: () -> Unit) =
    WnEmptyState(
        icon = Icons.AutoMirrored.Filled.MenuBook,
        title = "书架还是空的",
        body = "内置漫画源与书源已随应用提供，无需导入规则，也不用连接电脑。",
    ) {
        Button(onClick = onSearchManga, modifier = Modifier.testTag("empty_shelf_search_manga")) {
            Text("搜漫画")
        }
        OutlinedButton(onClick = onBrowseMangaSources,
            modifier = Modifier.testTag("empty_shelf_browse_manga")) {
            Text("浏览内置漫画源")
        }
        TextButton(onClick = onSearchNovel,
            modifier = Modifier.testTag("empty_shelf_search_novel")) {
            Text("搜小说（另一种内容类型）")
        }
    }

@Composable
private fun MangaCard(m: MangaItem, ep: EngineEndpoint, loader: ImageLoader,
                       updateLabel: String? = null, onClick: () -> Unit) {
    val shape = RoundedCornerShape(10.dp)
    Column(Modifier.clip(shape).border(1.dp, WnColors.line, shape)
               .clickable { onClick() }) {
        val url = if (m.coverPath.startsWith("/")) ep.imageOrigin + m.coverPath else m.coverPath
        if (url.isNotEmpty()) {
            AsyncImage(
                model = ImageRequest.Builder(LocalContext.current).data(url).build(),
                imageLoader = loader,
                contentDescription = m.title,
                modifier = Modifier.fillMaxWidth().height(120.dp).clip(shape)
                    .background(WnColors.surfaceVar),
                contentScale = androidx.compose.ui.layout.ContentScale.Crop,
            )
        } else {
            WnNoCover(Modifier.fillMaxWidth().height(120.dp).clip(shape))
        }
        Spacer(Modifier.height(WnSpace.xs))
        Column(Modifier.padding(horizontal = WnSpace.sm)) {
            Text(m.title, style = MaterialTheme.typography.bodySmall, maxLines = 2,
                overflow = TextOverflow.Ellipsis)
            Text(m.progressLabel, style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1,
                overflow = TextOverflow.Ellipsis)
            // 阅读进度条（对齐 web 端书库卡片）
            if (m.readRatio > 0) {
                Spacer(Modifier.height(WnSpace.xs))
                ShelfBar(m.readRatio, WnColors.accent)
            }
            if (updateLabel != null) {
                Text("有新话：" + updateLabel, style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.primary,
                    maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            Spacer(Modifier.height(WnSpace.sm))
        }
    }
}

@Composable
private fun SectionTitle(t: String) = WnSectionHeader(t)

@Composable
private fun ListRow(title: String, subtitle: String, modifier: Modifier = Modifier,
                    onClick: () -> Unit) =
    WnHairlineCard(modifier, onClick = onClick) {
        Column(Modifier.padding(WnSpace.md)) {
            Text(title, style = MaterialTheme.typography.bodyMedium, maxLines = 1,
                overflow = TextOverflow.Ellipsis)
            Text(subtitle, style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1,
                overflow = TextOverflow.Ellipsis)
        }
    }

// ── 浏览 / 下载 / 设置 ───────────────────────────────────────

@Composable
private fun BrowseScreen(gateway: EngineGateway, ep: EngineEndpoint,
                         onOpen: (Dest) -> Unit) {
    var sources by remember { mutableStateOf<List<MangaSource>>(emptyList()) }
    var msg by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    suspend fun reload() {
        val r = withContext(Dispatchers.IO) { gateway.httpText(ep.port, "/api/manga/sources") }
        sources = EngineData.sources(r.body)
    }
    LaunchedEffect(ep) { reload() }
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(12.dp),
               verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item { SectionTitle("漫画（优先）") }
        item {
            ListRow("搜漫画", "跨源搜索 → 点结果进原生详情页与阅读器") {
                onOpen(Dest.MangaSearch())
            }
        }
        item {
            ListRow("内置漫画源", "${sources.size} 个源：能力、分类与实测结论逐源可查") {
                onOpen(Dest.MangaSources)
            }
        }
        item {
            ListRow("探索（漫画分类 / 小说榜单）", "只列真有榜单或分类的源；无数据不做假榜单") {
                onOpen(Dest.Explore)
            }
        }
        item { SectionTitle("小说") }
        item {
            ListRow("搜小说", "跨源搜索 → 一键加入书架（进度在下载页）") {
                onOpen(Dest.NovelSearch)
            }
        }
        item {
            SectionTitle("漫画源能力（依赖判定与实测结论分开呈现，不假装全部可用）")
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(
                    onClick = {
                        scope.launch {
                            busy = true
                            msg = "正在启动漫画源验证…"
                            val start = gateway.httpPost(ep.port, "/api/manga/sources/verify",
                                org.json.JSONObject().put("limit", 4).toString())
                            val already = runCatching {
                                org.json.JSONObject(start.body).optBoolean("already_running")
                            }.getOrDefault(false)
                            if (already) {
                                msg = "已有一轮验证在跑，跟随它的进度"
                            } else if (start.code != 202 && start.code != 200) {
                                msg = "启动失败：HTTP ${start.code} ${start.body.take(80)}"
                            }
                            var done = false
                            for (i in 0 until 150) {
                                kotlinx.coroutines.delay(2000)
                                val st = gateway.httpText(ep.port, "/api/manga/sources/verify/status")
                                val o = runCatching {
                                    org.json.JSONObject(st.body)
                                }.getOrNull() ?: continue
                                val s2 = o.optString("status")
                                if (s2 == "running") {
                                    msg = "验证中 ${o.optInt("done")}/${o.optInt("total")}：" +
                                        o.optString("current")
                                    continue
                                }
                                if (s2 == "error") {
                                    msg = "验证异常：${o.optString("error")}"
                                    done = true
                                    break
                                }
                                done = true
                                break
                            }
                            if (!done) msg = "验证仍在后台进行（可稍后刷新）"
                            busy = false
                            reload()
                            if (done) msg = (msg ?: "") + " · 结果已更新"
                        }
                    },
                    enabled = !busy,
                ) { Text(if (busy) "验证中…" else "实测下一批漫画源") }
                Text(
                    "已实测通过 ${sources.count { it.verifyStatus == "verified" }} / " +
                        "共 ${sources.size}",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
            msg?.let {
                Spacer(Modifier.height(4.dp))
                Text(it, style = MaterialTheme.typography.labelSmall,
                     color = MaterialTheme.colorScheme.primary)
            }
            Spacer(Modifier.height(4.dp))
        }
        if (sources.isEmpty()) item { Text("（暂无源信息）", style = MaterialTheme.typography.bodySmall) }
        items(sources) { s ->
            // 过去这里是空回调（点了没反应）。现在进原生"源页"：
            // 能力/实测结论 + 该源自己声明的分类 → 分类结果 → 详情 → 阅读。
            ListRow("${s.name}  ·  ${statusLabel(s.status)}",
                "依赖：${s.reason.ifBlank { "满足" }}；实测：${s.verifyLabel}") {
                onOpen(Dest.MangaSource(s.key, s.name))
            }
        }
        item {
            Spacer(Modifier.height(6.dp))
            Text(
                "「实测」来自逐源功能验证（搜索 → 详情/目录 → 章节图片 → 真实取一张图）；" +
                    "没有实测过的源显示「未实测」，不代表不可用、也不代表可用。" +
                    "点上面的按钮即可在本机跑下一批实测，不需要打开网页。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }
    }
}

private fun statusLabel(s: String) = when (s) {
    "supported" -> "可用"
    "degraded" -> "部分可用"
    "unsupported" -> "不可用"
    "pending", "unknown" -> "待验证"
    else -> s
}

@Composable
private fun SettingsScreen(gateway: EngineGateway, ep: EngineEndpoint,
                           onOpen: (Dest) -> Unit) {
    val scope = rememberCoroutineScope()
    var summary by remember { mutableStateOf<Triple<String, String, List<String>>?>(null) }
    var busy by remember { mutableStateOf(false) }
    // SAF 保存回调与"生成按钮"不在同一作用域：待写入文本放这里（避免在回调里再发一次请求）
    var diagPending by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(ep) {
        val r = withContext(Dispatchers.IO) { gateway.httpText(ep.port, EngineGateway.STATUS_PATH) }
        summary = EngineData.engineSummary(r.body)
    }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(12.dp)) {
        SectionTitle("高级 · 诊断")
        val s = summary
        Text("引擎状态：${s?.first ?: "…"}    HTTP 服务：${s?.second ?: "…"}",
            style = MaterialTheme.typography.bodySmall)
        Text("端口：${ep.port}    实例：${ep.instanceId.take(8)}",
            style = MaterialTheme.typography.bodySmall)
        Text("Python：3.13（内嵌）", style = MaterialTheme.typography.bodySmall)
        // 产物基线（P0-A）：版本 + 源码 revision + 构建时间，用户截图可直接核对
        Text(BuildInfo.summary, style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.outline,
            modifier = Modifier.testTag("build_info"))
        if (!s?.third.isNullOrEmpty()) {
            Spacer(Modifier.height(6.dp))
            Text("缺少的可选能力：${s!!.third.joinToString("、")}",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error)
            Text("受影响的源已标注为「部分可用/不可用」；小说搜索与已下载内容不受影响。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline)
        }
        Spacer(Modifier.height(8.dp))
        // 导出脱敏诊断（方向基线 §7.1）：用户在手机上导一次，就能定位故障分层
        var diagMsg by remember { mutableStateOf<String?>(null) }
        // 一键测速结果（0.71.0）：在真机上量出「哪一段慢、走哪条传输」，
        // 用户点一下就拿到可行动的数字，不必靠复述。
        var speedMsg by remember { mutableStateOf<String?>(null) }
        var speedBusy by remember { mutableStateOf(false) }
        var diagBusy by remember { mutableStateOf(false) }
        val diagCtx = LocalContext.current
        val diagSaver = rememberLauncherForActivityResult(
            ActivityResultContracts.CreateDocument("text/plain")
        ) { uri: android.net.Uri? ->
            val pending = diagPending
            if (uri == null) {
                diagMsg = "已取消导出"
            } else if (pending == null) {
                diagMsg = "没有待写入的诊断文本（请重试）"
            } else {
                scope.launch {
                    diagMsg = try {
                        val bytes = withContext(Dispatchers.IO) {
                            val out = diagCtx.contentResolver.openOutputStream(uri, "w")
                                ?: throw IllegalStateException("系统未返回可写流")
                            out.use { it.write(pending.toByteArray(Charsets.UTF_8)) }
                            pending.toByteArray(Charsets.UTF_8).size
                        }
                        "诊断报告已保存（${Export.human(bytes.toLong())}）；" +
                            "把它发出来即可定位故障分层"
                    } catch (t: Throwable) {
                        "保存失败：${t.message ?: t.javaClass.simpleName}"
                    }
                }
            }
        }
        OutlinedButton(
            onClick = {
                diagBusy = true; diagMsg = "正在收集诊断信息…"
                scope.launch {
                    val r = gateway.httpText(ep.port, "/api/diagnostics/report")
                    if (!r.ok) {
                        val why = runCatching {
                            org.json.JSONObject(r.body).optString("error")
                        }.getOrDefault("")
                        diagMsg = "生成失败：HTTP ${r.code}" +
                            (if (why.isNotBlank()) " · $why" else "")
                        diagBusy = false
                        return@launch
                    }
                    val serverText = runCatching {
                        org.json.JSONObject(r.body).optString("text")
                    }.getOrDefault("")
                    if (serverText.isBlank()) {
                        diagMsg = "生成失败：服务端返回空报告"
                        diagBusy = false
                        return@launch
                    }
                    // App 侧补上产物基线与设备信息（服务端看不到这些）
                    val head = buildString {
                        append("=== 应用与设备（App 侧补充）===").append('\n')
                        append(BuildInfo.summary).append('\n')
                        append("设备：").append(android.os.Build.MANUFACTURER).append(' ')
                            .append(android.os.Build.MODEL).append('\n')
                        append("系统：Android ").append(android.os.Build.VERSION.RELEASE)
                            .append("（API ").append(android.os.Build.VERSION.SDK_INT).append("）")
                            .append(" · ").append(android.os.Build.SUPPORTED_ABIS.joinToString(","))
                            .append('\n')
                        append("引擎地址：127.0.0.1:").append(ep.port)
                            .append("（实例 ").append(ep.instanceId.take(8)).append("）").append('\n')
                        append('\n')
                    }
                    diagPending = head + serverText
                    diagBusy = false
                    val name = Export.safeFileName(
                        "webnovel诊断-${BuildConfig.VERSION_NAME}-" +
                            java.text.SimpleDateFormat("yyyyMMdd-HHmm", java.util.Locale.US)
                                .format(java.util.Date()) + ".txt",
                        "webnovel-diagnostics.txt")
                    diagSaver.launch(name)
                }
            },
            enabled = !diagBusy,
            modifier = Modifier.testTag("export_diagnostics"),
        ) { Text(if (diagBusy) "收集中…" else "导出诊断报告") }
        diagMsg?.let {
            Spacer(Modifier.height(4.dp))
            Text(it, style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline)
        }
        Spacer(Modifier.height(8.dp))
        OutlinedButton(
            onClick = {
                speedBusy = true; speedMsg = null
                scope.launch {
                    val r = gateway.httpText(ep.port, "/api/diagnostics/latency?source=jm")
                    speedMsg = if (r.ok) {
                        // 只取 text 字段（用全限定名，避免再加 import）
                        val txt = runCatching {
                            org.json.JSONObject(r.body).optString("text")
                        }.getOrNull().orEmpty()
                        txt.ifBlank { r.body.take(300) }
                    } else {
                        "测速失败：HTTP ${r.code}" + if (r.body.isNotBlank()) " · ${r.body.take(120)}" else ""
                    }
                    speedBusy = false
                }
            },
            enabled = !speedBusy,
            modifier = Modifier.testTag("measure_latency"),
        ) { Text(if (speedBusy) "测速中…（最多约 30 秒）" else "一键测速（禁漫阅读链路）") }
        speedMsg?.let {
            Spacer(Modifier.height(4.dp))
            Text(it, style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline)
        }
        Spacer(Modifier.height(12.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = {
                busy = true
                scope.launch {
                    gateway.stopEngine(); busy = false
                }
            }, enabled = !busy) { Text("停止引擎") }
            OutlinedButton(onClick = {
                busy = true
                scope.launch {
                    withContext(Dispatchers.IO) {
                        gateway.httpText(ep.port, "/__mobile/stop")
                    }
                    busy = false
                }
            }, enabled = !busy) { Text("重启引擎") }
        }
        Spacer(Modifier.height(12.dp))
        SectionTitle("任务恢复（引擎重启/被系统限制之后）")
        var tasks by remember { mutableStateOf<List<EngineData.TaskItem>>(emptyList()) }
        var recMsg by remember { mutableStateOf<String?>(null) }
        var recBusy by remember { mutableStateOf(false) }
        LaunchedEffect(ep) {
            val r = withContext(Dispatchers.IO) { gateway.httpText(ep.port, "/api/tasks") }
            tasks = EngineData.tasks(r.body)
        }
        val recoverable = tasks.filter { it.status == "paused" || it.status == "stopped" || it.status == "error" }
        Text(
            if (tasks.isEmpty()) "当前没有任务记录"
            else "可恢复任务 ${recoverable.size} 个（小说 ${recoverable.count { !it.isManga }} / " +
                "漫画 ${recoverable.count { it.isManga }}）；进行中 ${tasks.count { it.running }} 个",
            style = MaterialTheme.typography.bodySmall,
        )
        Text(
            "引擎重启会把中断的任务收敛为「已停止」并保留记录，不会自动续跑——" +
                "是否继续由你决定，避免在不知情的情况下消耗流量。",
            style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline,
        )
        Spacer(Modifier.height(6.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(
                onClick = {
                    recBusy = true
                    scope.launch {
                        var novel = 0
                        var manga = 0
                        for (t in recoverable) {
                            if (t.isManga) {
                                val r = gateway.httpPost(ep.port,
                                    "/api/manga/download/resume?source=${Uri.encode(t.mangaSource)}" +
                                        "&cid=${Uri.encode(t.mangaComicId)}")
                                if (r.ok) manga++
                            } else {
                                val r = gateway.httpPost(ep.port,
                                    "/api/tasks/${Uri.encode(t.id)}/resume")
                                if (r.ok) novel++
                            }
                        }
                        recMsg = "已请求恢复：小说 $novel 个、漫画 $manga 个（失败的不计入）"
                        val r = gateway.httpText(ep.port, "/api/tasks")
                        tasks = EngineData.tasks(r.body)
                        recBusy = false
                    }
                },
                enabled = !recBusy && recoverable.isNotEmpty(),
            ) { Text(if (recBusy) "恢复中…" else "全部恢复（${recoverable.size}）") }
            OutlinedButton(onClick = {
                scope.launch {
                    val r = gateway.httpText(ep.port, "/api/tasks")
                    tasks = EngineData.tasks(r.body)
                    recMsg = "已刷新任务列表"
                }
            }, enabled = !recBusy) { Text("刷新") }
        }
        recMsg?.let {
            Spacer(Modifier.height(4.dp))
            Text(it, style = MaterialTheme.typography.labelSmall,
                 color = MaterialTheme.colorScheme.primary)
        }

        Spacer(Modifier.height(12.dp))
        SectionTitle("阅读设置")
        ListRow("阅读设置", "字号/行距/主题；漫画纵向连续或横向翻页") {
            onOpen(Dest.ReaderPrefs)
        }
        Text("设置立刻生效并保存在本机，引擎重启或失败都不会丢。",
            style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline)

        Spacer(Modifier.height(12.dp))
        SectionTitle("书源与数据")
        ListRow("书源管理", "启用/停用、逐源校验、从文件导入、删除") {
            onOpen(Dest.Sources)
        }
        ListRow("存储管理", "按源/作品/章节看占用；选择性清理错误缓存") {
            onOpen(Dest.Storage)
        }
        ListRow("备份与恢复", "书源配置 + 阅读进度（不含正文与图片）") {
            onOpen(Dest.Backup)
        }

        Spacer(Modifier.height(12.dp))
        SectionTitle("高级（网页诊断）")
        Text("下面这个网页只是诊断与兜底入口（书源规则编辑、任务原始列表），" +
            "不是日常使用界面：搜索、详情、阅读、下载、浏览都走原生页面，不需要网页。",
            style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline)
        Spacer(Modifier.height(6.dp))
        ListRow("打开网页诊断页", "带 ?mobile=1：不加载桌面局域网地址提示") {
            onOpen(Dest.Web("/?mobile=1", "网页诊断"))
        }
    }
}

// ── 内嵌阅读器（仅回环 + 已注入会话 Cookie）──────────────────

@SuppressLint("SetJavaScriptEnabled")
@Composable
private fun ReaderWebView(gateway: EngineGateway, ep: EngineEndpoint, path: String,
                          title: String, onBack: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var cookieReady by remember { mutableStateOf(false) }

    // 复用网关唯一的 Cookie 注入实现（等待回调完成；不在此处复制一份属性串）
    LaunchedEffect(ep, path) { cookieReady = gateway.installCookie(ep) }

    Scaffold(topBar = {
        TopAppBar(title = { Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis) },
            navigationIcon = { TextButton(onClick = onBack) { Text("← 返回") } })
    }) { pad ->
        Box(Modifier.padding(pad).fillMaxSize()) {
            if (!cookieReady) {
                Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
            } else {
                AndroidView(factory = { ctx ->
                    WebView(ctx).apply {
                        settings.javaScriptEnabled = true
                        settings.domStorageEnabled = true
                        settings.allowFileAccess = false
                        settings.allowContentAccess = false
                        settings.cacheMode = WebSettings.LOAD_DEFAULT
                        settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
                        webViewClient = object : WebViewClient() {
                            override fun shouldOverrideUrlLoading(v: WebView, req: WebResourceRequest): Boolean {
                                val u = req.url
                                if (u.host == "127.0.0.1" || u.host == "localhost") return false
                                return try {
                                    ctx.startActivity(android.content.Intent(
                                        android.content.Intent.ACTION_VIEW, u)); true
                                } catch (t: Throwable) { true }
                            }
                            override fun onReceivedSslError(v: WebView, h: SslErrorHandler,
                                                            e: android.net.http.SslError) {
                                h.cancel()   // 证书错误直接拒绝（设计 §8）
                            }
                        }
                        // 手机网页一律带 mobile=1：不加载桌面局域网地址发现脚本
                        // （那条"当前页面地址已失效"的误报来自它）。
                        val sep = if (path.contains("?")) "&" else "?"
                        loadUrl(ep.origin + path + sep + "mobile=1")
                    }
                }, modifier = Modifier.fillMaxSize())
            }
        }
    }
}
