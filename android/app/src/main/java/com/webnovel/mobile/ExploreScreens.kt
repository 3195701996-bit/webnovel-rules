@file:OptIn(ExperimentalMaterial3Api::class)

package com.webnovel.mobile

import android.net.Uri
import androidx.compose.foundation.background
import androidx.compose.foundation.border
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
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.Card
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
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.ImageLoader
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * 探索（榜单/分类），对齐 Venera 的"探索"分页——但**只做有数据支撑的探索**：
 *
 * - 只列出声明了 exploreUrl/ruleExplore 的书源；没有的源不出现，
 *   更不拿搜索结果或推荐位冒充榜单（诊断 §4 明确禁止）。
 * - 分类来自源自己的 exploreUrl，标题与分组照原样展示。
 * - 榜单书单可一键「加入书架」（创建下载任务），进度在下载页看。
 * - 取数失败时显示服务端给的原因（例如目标被 SSRF 拒绝 / 站点结构变化），
 *   不用空列表掩盖失败。
 */
@Composable
internal fun ExploreScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    loader: ImageLoader,
    onOpen: (Dest) -> Unit,
    onBack: () -> Unit,
) {
    // P0-E：源清单、选中的源/分类、已加载书单都保存"原始响应 + 选择标识"，
    // 从详情或结果页返回时原样恢复（含滚动位置，由 SaveableStateHolder 恢复
    // rememberLazyListState）。保存原始响应而不是解析后的对象：恢复逻辑与首次
    // 解析走同一套防御式解析器，不需要自定义 Saver。
    var sourcesBody by rememberSaveable { mutableStateOf("") }
    var currentUid by rememberSaveable { mutableStateOf("") }
    var categoryUrl by rememberSaveable { mutableStateOf("") }
    var bookPages by rememberSaveable { mutableStateOf(arrayListOf<String>()) }
    var loading by remember { mutableStateOf(true) }
    var loadingBooks by remember { mutableStateOf(false) }
    var page by rememberSaveable { mutableIntStateOf(1) }
    var error by rememberSaveable { mutableStateOf("") }
    var msg by rememberSaveable { mutableStateOf("") }
    val scope = rememberCoroutineScope()

    val (sources, counts) = remember(sourcesBody) {
        if (sourcesBody.isBlank()) emptyList<ExploreSource>() to (0 to 0)
        else EngineData.exploreSources(sourcesBody)
    }
    val mangaNote = remember(sourcesBody) {
        if (sourcesBody.isBlank()) false to "" else EngineData.exploreMangaNote(sourcesBody)
    }
    val mangaSources = remember(sourcesBody) {
        if (sourcesBody.isBlank()) emptyList()
        else EngineData.exploreMangaSources(sourcesBody)
    }
    val current = remember(sources, currentUid) {
        sources.firstOrNull { it.uid == currentUid }
    }
    val category = remember(current, categoryUrl) {
        current?.categories?.firstOrNull { it.url == categoryUrl }
    }
    // 选中的漫画分类（源 + 分类）：同样按"标识"保存，非空时进入独立结果视图。
    // 不做成"结果追加在当前列表下方"——19 个分类按钮之后的结果等于没显示。
    var mangaSelSrc by rememberSaveable { mutableStateOf("") }
    var mangaSelCat by rememberSaveable { mutableStateOf("") }
    val mangaSel: Pair<EngineData.MangaBrowseSource, ExploreCategory>? =
        remember(mangaSources, mangaSelSrc, mangaSelCat) {
            val ms = mangaSources.firstOrNull { it.key == mangaSelSrc }
            val c = ms?.categories?.firstOrNull { it.url == mangaSelCat }
            if (ms != null && c != null) ms to c else null
        }

    val books: List<ExploreBook> = remember(bookPages.size) {
        bookPages.flatMap { EngineData.exploreBooks(it) }
            .distinctBy { it.sourceUid + "|" + it.bookUrl }
    }

    suspend fun loadSources() {
        loading = true; error = ""
        val r = gateway.httpText(ep.port, "/api/explore/sources")
        if (!r.ok) {
            error = "读取探索源失败：HTTP ${r.code}"
        } else {
            sourcesBody = r.body
        }
        loading = false
    }
    // 已有清单就不再重拉（返回时不能被一次刷新盖掉）；需要最新时用「重试/刷新」
    LaunchedEffect(ep) { if (sourcesBody.isBlank()) loadSources() else loading = false }

    fun loadBooks(src: ExploreSource, cat: ExploreCategory, nextPage: Int) {
        scope.launch {
            loadingBooks = true; error = ""; msg = ""
            val r = gateway.httpText(ep.port,
                "/api/explore?source=${Uri.encode(src.uid)}&url=${Uri.encode(cat.url)}&page=$nextPage")
            if (!r.ok) {
                val why = runCatching { JSONObject(r.body).optString("error") }.getOrDefault("")
                error = "读取榜单失败：HTTP ${r.code}" + if (why.isNotBlank()) " · $why" else ""
                if (nextPage == 1) bookPages = arrayListOf()
            } else {
                val next = if (nextPage == 1) arrayListOf<String>() else ArrayList(bookPages)
                next.add(r.body)
                bookPages = next
                page = nextPage
                if (EngineData.exploreBooks(r.body).isEmpty() && nextPage == 1) {
                    msg = "该分类本次没有返回内容"
                }
            }
            loadingBooks = false
        }
    }

    Scaffold(topBar = {
        TopAppBar(
            title = {
                Text(
                    when {
                        mangaSel != null ->
                            "${mangaSel.first.name} · ${mangaSel.second.title}"
                        category != null -> category.title
                        current != null -> current.name
                        else -> "探索（榜单/分类）"
                    },
                    maxLines = 1, overflow = TextOverflow.Ellipsis,
                )
            },
            navigationIcon = {
                IconButton(onClick = {
                    when {
                        mangaSel != null -> { mangaSelSrc = ""; mangaSelCat = "" }
                        categoryUrl.isNotBlank() -> { categoryUrl = ""; bookPages = arrayListOf() }
                        current != null -> currentUid = ""
                        else -> onBack()
                    }
                }) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                }
            },
            actions = {
                if (category != null) {
                    TextButton(onClick = {
                        current?.let { c -> category?.let { k -> loadBooks(c, k, page + 1) } }
                    }, enabled = !loadingBooks) { Text("下一页") }
                }
            },
        )
    }) { pad ->
        Box(Modifier.padding(pad).fillMaxSize()) {
            when {
                loading -> Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
                // 漫画分类结果：独立一屏（自己的加载/失败/空态自己说明）
                mangaSel != null -> MangaBrowseResultsScreen(
                    gateway, ep, loader, mangaSel.first, mangaSel.second, onOpen)
                // error 现在是 String（可保存）：必须用 isNotBlank 判断，
                // 写成 `!= null` 会恒为真 → 整页被错误屏盖住（实测踩到）
                error.isNotBlank() -> Column(Modifier.fillMaxSize().padding(WnSpace.xl), Arrangement.Center) {
                    Text(error, color = MaterialTheme.colorScheme.error)
                    Spacer(Modifier.height(WnSpace.md))
                    Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                        Button(onClick = {
                            scope.launch { loadSources() }
                        }) { Text("重试") }
                        if (category != null && current != null) {
                            OutlinedButton(onClick = { loadBooks(current, category, 1) }) {
                                Text("重试本分类")
                            }
                        }
                    }
                }
                // 三级：源 → 分类 → 书单
                category != null -> Column(Modifier.fillMaxSize()) {
                    if (msg.isNotBlank()) {
                        Text(msg, style = MaterialTheme.typography.labelMedium,
                             color = MaterialTheme.colorScheme.outline,
                             modifier = Modifier.padding(WnSpace.md))
                    }
                    if (loadingBooks && books.isEmpty()) {
                        Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
                    } else {
                        LazyColumn(Modifier.fillMaxSize().testTag("explore_books"),
                            contentPadding = PaddingValues(WnSpace.md),
                            verticalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                            items(books, key = { it.sourceUid + "|" + it.bookUrl }) { b ->
                                Card(Modifier.fillMaxWidth()) {
                                    Row(Modifier.padding(WnSpace.md), verticalAlignment = Alignment.CenterVertically) {
                                        Column(Modifier.weight(1f)) {
                                            Text(b.name.ifBlank { "（无书名）" },
                                                style = MaterialTheme.typography.bodyMedium,
                                                fontWeight = FontWeight.Medium, maxLines = 2,
                                                overflow = TextOverflow.Ellipsis)
                                            Text(
                                                listOfNotNull(
                                                    b.author.takeIf { it.isNotBlank() },
                                                    b.lastChapter.takeIf { it.isNotBlank() },
                                                ).joinToString(" · ").ifBlank { b.sourceName },
                                                style = MaterialTheme.typography.labelSmall,
                                                color = MaterialTheme.colorScheme.outline,
                                                maxLines = 1, overflow = TextOverflow.Ellipsis)
                                        }
                                        OutlinedButton(onClick = {
                                            scope.launch {
                                                val body = JSONObject()
                                                    .put("source_uid", b.sourceUid)
                                                    .put("book_url", b.bookUrl).toString()
                                                val r = gateway.httpPost(ep.port, "/api/tasks", body)
                                                msg = if (r.code == 202) {
                                                    "已加入书架并开始下载：${b.name}"
                                                } else {
                                                    "加入书架失败：HTTP ${r.code} " + r.body.take(100)
                                                }
                                            }
                                        }) { Text("加入书架") }
                                    }
                                }
                            }
                        }
                    }
                }
                current != null -> {
                    val src = current
                    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(WnSpace.md)) {
                        Text("${src.name} · ${src.categories.size} 个分类",
                             style = MaterialTheme.typography.labelMedium,
                             color = MaterialTheme.colorScheme.outline)
                        Spacer(Modifier.height(WnSpace.sm))
                        src.categories.forEach { c ->
                            // 分组标题只在首次出现时显示（源自己的分组结构照原样）
                            val idx = src.categories.indexOf(c)
                            if (c.group.isNotBlank() &&
                                (idx == 0 || src.categories[idx - 1].group != c.group)) {
                                Text(c.group, style = MaterialTheme.typography.titleSmall,
                                     fontWeight = FontWeight.Bold,
                                     modifier = Modifier.padding(top = WnSpace.sm, bottom = WnSpace.xs))
                            }
                            OutlinedButton(
                                onClick = { categoryUrl = c.url; loadBooks(src, c, 1) },
                                modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp),
                                contentPadding = PaddingValues(vertical = 6.dp),
                            ) { Text(c.title, maxLines = 1, overflow = TextOverflow.Ellipsis) }
                        }
                    }
                }
                // 两个列表**都**为空才算"没有可探索的源"：小说侧没有榜单而漫画侧
                // 有分类时，也必须能进到漫画分类（否则漫画探索被空态挡住看不见）。
                sources.isEmpty() && mangaSources.isEmpty() -> Column(
                    Modifier.fillMaxSize().padding(WnSpace.xl),
                    Arrangement.Center, Alignment.CenterHorizontally) {
                    Text("没有可探索的源", style = MaterialTheme.typography.titleMedium)
                    Spacer(Modifier.height(6.dp))
                    Text(
                        "探索页只展示自己声明了榜单/分类规则的书源；" +
                            "当前 ${counts.first} 个内置源里只有 ${counts.second} 个声明了探索规则。" +
                            "没有数据的源不会被编造出榜单。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.outline,
                    )
                    if (mangaNote.second.isNotBlank()) {
                        Spacer(Modifier.height(6.dp))
                        Text("漫画侧：" + mangaNote.second,
                             style = MaterialTheme.typography.bodySmall,
                             color = MaterialTheme.colorScheme.outline)
                    }
                }
                else -> LazyColumn(Modifier.fillMaxSize().testTag("explore_sources"),
                    contentPadding = PaddingValues(WnSpace.md),
                    verticalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                    item {
                        Text(
                            "漫画：" + (if (mangaNote.first) "见下方漫画分类"
                                       else mangaNote.second.ifBlank { "当前不提供探索" }),
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.outline,
                        )
                        Spacer(Modifier.height(WnSpace.xs))
                        Text(
                            "小说：${counts.second} / ${counts.first} 个内置源提供榜单或分类；" +
                                "其余源不提供，不做假榜单。",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.outline,
                        )
                    }
                    if (mangaSources.isNotEmpty()) {
                        item {
                            Spacer(Modifier.height(WnSpace.xs))
                            Text("漫画分类（来自适配器自己声明的排名/分类）",
                                 style = MaterialTheme.typography.titleSmall,
                                 fontWeight = FontWeight.Bold)
                        }
                        items(mangaSources, key = { "manga_src_" + it.key }) { ms ->
                            Column(Modifier.fillMaxWidth().padding(top = 6.dp)) {
                                Text("${ms.name} · ${ms.categories.size} 个入口",
                                     style = MaterialTheme.typography.bodyMedium,
                                     fontWeight = FontWeight.Medium)
                                // 分组标题由适配器自己声明（排行/分类），只在变化处显示
                                ms.categories.forEachIndexed { idx, c ->
                                    if (c.group.isNotBlank() &&
                                        (idx == 0 || ms.categories[idx - 1].group != c.group)) {
                                        Text(c.group,
                                             style = MaterialTheme.typography.labelMedium,
                                             color = MaterialTheme.colorScheme.outline,
                                             modifier = Modifier.padding(top = 6.dp, bottom = 2.dp))
                                    }
                                    OutlinedButton(
                                        onClick = { mangaSelSrc = ms.key; mangaSelCat = c.url },
                                        // 分类按钮带稳定 testTag（<源key>_<分类key>）：
                                        // 界面验收要能精确点到某个分类，而不是靠文字猜
                                        modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp)
                                            .testTag("manga_cat_" + ms.key + "_" + c.url),
                                        contentPadding = PaddingValues(vertical = 6.dp),
                                    ) { Text(c.title, maxLines = 1, overflow = TextOverflow.Ellipsis) }
                                }
                            }
                        }
                    }
                    items(sources, key = { it.uid + "|" + it.name }) { s ->
                        Card(Modifier.fillMaxWidth().clickable { currentUid = s.uid }) {
                            Column(Modifier.padding(WnSpace.md)) {
                                Text(s.name, style = MaterialTheme.typography.bodyMedium,
                                     fontWeight = FontWeight.Medium)
                                Text(
                                    listOfNotNull(
                                        "${s.categories.size} 个分类入口",
                                        if (!s.enabled) "（该源当前已停用）" else null,
                                    ).joinToString(" · "),
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.outline,
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}

/**
 * 漫画分类结果（独立一屏）。
 *
 * 为什么单独一屏：分类按钮有近 20 个，把结果追加在按钮下方等于"点了没反应"。
 * 这里把加载/失败/空态都说清楚，并且卡片可点进原生漫画详情（只列标题等于半成品）。
 * 翻页按 (source, comicId) 去重——各源 page 口径不同，重复项会铺满屏。
 */
@Composable
internal fun MangaBrowseResultsScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    loader: ImageLoader,
    src: EngineData.MangaBrowseSource,
    cat: ExploreCategory,
    onOpen: (Dest) -> Unit,
) {
    // P0-E：结果按"已加载页的原始响应"保存——从详情返回时结果还在、
    // 页码与滚动位置（rememberLazyListState 由 SaveableStateHolder 恢复）也在。
    var pages by rememberSaveable(src.key, cat.url) { mutableStateOf(arrayListOf<String>()) }
    var error by rememberSaveable(src.key, cat.url) { mutableStateOf("") }
    var page by rememberSaveable(src.key, cat.url) { mutableIntStateOf(1) }
    var loading by remember { mutableStateOf(true) }
    var more by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    val hits: List<MangaSearchHit> = remember(pages.size) {
        pages.fold(emptyList<MangaSearchHit>()) { acc, body ->
            val (h, _) = EngineData.mangaSearch(body)
            val seen = acc.map { it.source + ":" + it.comicId }.toSet()
            acc + h.filter { (it.source + ":" + it.comicId) !in seen }
        }
    }

    suspend fun load(nextPage: Int) {
        if (nextPage == 1) loading = true else more = true
        error = ""
        val r = gateway.httpText(ep.port,
            "/api/manga/browse?source=${Uri.encode(src.key)}" +
                "&category=${Uri.encode(cat.url)}&page=$nextPage")
        if (!r.ok) {
            val why = runCatching { JSONObject(r.body).optString("error") }.getOrDefault("")
            error = "读取分类失败：HTTP ${r.code}" + if (why.isNotBlank()) " · $why" else ""
            if (nextPage == 1) pages = arrayListOf()
        } else {
            val next = if (nextPage == 1) arrayListOf<String>() else ArrayList(pages)
            next.add(r.body)
            pages = next
            page = nextPage
        }
        loading = false; more = false
    }
    // 已有结果（从详情返回时）就不再重拉：否则"返回恢复"立刻被一次网络刷新盖掉
    LaunchedEffect(src.key, cat.url) { if (pages.isEmpty()) load(1) else loading = false }

    Column(Modifier.fillMaxSize()) {
        Text(
            listOfNotNull(
                "${src.name} · ${cat.title}",
                if (hits.isNotEmpty()) "${hits.size} 部" else null,
                if (hits.isNotEmpty()) "第 $page 页" else null,
            ).joinToString(" · "),
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.outline,
            modifier = Modifier.padding(start = WnSpace.md, top = WnSpace.sm, end = WnSpace.md)
                .testTag("manga_browse_caption"),
        )
        when {
            loading && hits.isEmpty() ->
                Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
            error.isNotBlank() && hits.isEmpty() -> Column(
                Modifier.fillMaxSize().padding(WnSpace.xl), Arrangement.Center) {
                Text(error, color = MaterialTheme.colorScheme.error)
                Spacer(Modifier.height(WnSpace.md))
                Button(onClick = { scope.launch { load(1) } }) { Text("重试") }
            }
            hits.isEmpty() -> Text(
                "「${cat.title}」本次没有取到内容（源站可能暂时不可达，稍后再试）。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.outline,
                modifier = Modifier.padding(WnSpace.lg),
            )
            else -> LazyColumn(Modifier.fillMaxSize().testTag("manga_browse_list"),
                contentPadding = PaddingValues(WnSpace.md),
                verticalArrangement = Arrangement.spacedBy(6.dp)) {
                items(hits.chunked(3), key = { row -> row.joinToString("|") { it.source + it.comicId } }) { row ->
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        row.forEach { h ->
                            MangaBrowseCard(h, ep, loader, Modifier.weight(1f)) {
                                onOpen(Dest.MangaDetail(h.source, h.comicId))
                            }
                        }
                        // 末行不足 3 个时补空位，避免卡片被拉宽
                        repeat(3 - row.size) { Spacer(Modifier.weight(1f)) }
                    }
                }
                item {
                    Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm),
                        modifier = Modifier.padding(top = 6.dp)) {
                        OutlinedButton(enabled = !more, onClick = {
                            scope.launch { load(page + 1) }
                        }) { Text(if (more) "加载中…" else "加载更多") }
                    }
                }
                if (error.isNotBlank()) {
                    item {
                        Text(error, style = MaterialTheme.typography.labelSmall,
                             color = MaterialTheme.colorScheme.error)
                    }
                }
            }
        }
    }
}

/**
 * 漫画浏览结果卡片：封面走服务端封面接口（本机回环，token 由 loader 注入请求头），
 * 点一下进原生漫画详情——浏览结果必须能接着往下走，只显示标题等于半成品。
 * 封面取不到就显示占位（不假装有图）。
 */
@Composable
private fun MangaBrowseCard(
    h: MangaSearchHit,
    ep: EngineEndpoint,
    loader: ImageLoader,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    // 浏览结果不在书库里 → per-comic 封面必 404；走服务端代理（带源站防盗链头）
    val cover = remoteCoverUrl(ep, h.source, h.cover)
    Column(modifier.clip(WnChipShape)
        .border(1.dp, WnColors.line, WnChipShape)
        .clickable { onClick() }
        .testTag("manga_browse_card")) {
        WnCoverImage(
            url = cover,
            contentDescription = h.title,
            loader = loader,
            modifier = Modifier.fillMaxWidth().height(130.dp),
        )
        Spacer(Modifier.height(WnSpace.xs))
        Text(h.title.ifBlank { "（无标题）" },
            style = MaterialTheme.typography.bodySmall, maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(horizontal = WnSpace.sm))
        Text(
            listOfNotNull(
                h.author.take(10).takeIf { it.isNotBlank() },
                h.sourceName.takeIf { it.isNotBlank() },
            ).joinToString(" · "),
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.outline,
            maxLines = 1, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(horizontal = WnSpace.sm, vertical = 2.dp),
        )
    }
}
