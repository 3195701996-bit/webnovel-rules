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
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import android.net.Uri
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Scaffold
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
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.ImageLoader
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * 内置漫画源（原生）：源清单 → 单源页 → 该源的分类 → 分类结果 → 详情 → 阅读。
 *
 * 为什么要有这两个页面（方向基线 P0-D）：
 * 浏览页里的漫画源卡片过去是**空回调**（点了没有任何反应），而"内置源"又是本产品
 * 与 Venera 的关键差异——用户需要能看到每个源到底能干什么、实测结论是什么、
 * 有哪些分类可浏览。空按钮比没有按钮更糟：用户会以为应用坏了。
 *
 * 诚实口径：能力（依赖判定）与实测结论分开显示；没有实测就说"未实测"，
 * 没有分类就说"该源未提供排行/分类"，绝不拿搜索结果冒充分类。
 */

private data class MangaSourceCard(
    val key: String,
    val name: String,
    val status: String,
    val reason: String,
    val verifyLabel: String,
    /** 混淆源（jm 等）：图片由服务器做块还原，算法升级后需要能重建旧缓存 */
    val scrambled: Boolean = false,
    val processVersion: Int = 0,
    /** 移动可用性分类（路线 §4.4）：已验证/降级/不支持/待验证 */
    val categoryLabel: String = "",
    val categoryReason: String = "",
    /** 实测最早失败的阶段 */
    val failedStage: String = "",
)

/** 内置漫画源清单（也是空书架"浏览内置漫画源"的落点） */
@Composable
internal fun MangaSourceListScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    onOpen: (Dest) -> Unit,
    onBack: () -> Unit,
) {
    var body by rememberSaveable { mutableStateOf("") }
    var error by rememberSaveable { mutableStateOf("") }
    var loading by remember { mutableStateOf(true) }

    val sources: List<MangaSourceCard> = remember(body) {
        if (body.isBlank()) emptyList()
        else EngineData.sources(body).map {
            MangaSourceCard(it.key, it.name, it.status, it.reason, it.verifyLabel)
        }
    }
    LaunchedEffect(ep) {
        if (body.isBlank()) {
            val r = gateway.httpText(ep.port, "/api/manga/sources")
            if (r.ok) body = r.body else error = "读取漫画源失败：HTTP ${r.code}"
        }
        loading = false
    }

    Scaffold(topBar = {
        TopAppBar(title = { Text("内置漫画源", maxLines = 1) },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                }
            })
    }) { pad ->
        Box(Modifier.padding(pad).fillMaxSize()) {
            when {
                loading -> Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
                error.isNotBlank() -> Column(Modifier.fillMaxSize().padding(WnSpace.xl), Arrangement.Center) {
                    Text(error, color = MaterialTheme.colorScheme.error)
                }
                else -> LazyColumn(Modifier.fillMaxSize().testTag("manga_source_list"),
                    contentPadding = PaddingValues(WnSpace.md),
                    verticalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                    item {
                        Text("这些源随应用提供，不需要导入规则。点进去看它的能力、" +
                            "实测结论和可浏览的分类。",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    items(sources, key = { it.key }) { s ->
                        WnHairlineCard(onClick = {
                            onOpen(Dest.MangaSource(s.key, s.name))
                        }) {
                            Column(Modifier.padding(WnSpace.md)) {
                                Text(s.name, style = MaterialTheme.typography.bodyMedium,
                                    fontWeight = FontWeight.Medium)
                                Text(
                                    "依赖：${s.reason.ifBlank { "满足" }}；实测：${s.verifyLabel}",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}

/** 单源页：能力 + 实测结论 + 该源声明的分类（有分类时可直接进去浏览） */
@Composable
internal fun MangaSourceScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    loader: ImageLoader,
    sourceKey: String,
    sourceName: String,
    onOpen: (Dest) -> Unit,
    onBack: () -> Unit,
) {
    // P0-E：卡片数据与"选中的分类"都可恢复——从分类结果点进详情再返回时，
    // 仍停在原来的分类结果里，而不是被打回源清单。
    // 保存的是**原始响应**：恢复时复用同一套防御式解析，不需要自定义 Saver。
    var sourcesBody by rememberSaveable(sourceKey) { mutableStateOf("") }
    var catsBody by rememberSaveable(sourceKey) { mutableStateOf("") }
    var selUrl by rememberSaveable(sourceKey) { mutableStateOf("") }
    var loading by remember { mutableStateOf(true) }
    // 重建图片缓存（混淆源 jm 等）：算法修好后，旧缓存里的花图必须能少代价重建
    var rebuildBusy by remember { mutableStateOf(false) }
    var rebuildDialog by remember { mutableStateOf(false) }
    var rebuildMsg by rememberSaveable(sourceKey) { mutableStateOf("") }
    val scope = rememberCoroutineScope()

    val card: MangaSourceCard? = remember(sourcesBody) {
        if (sourcesBody.isBlank()) null
        else EngineData.sources(sourcesBody).map {
            MangaSourceCard(it.key, it.name, it.status, it.reason, it.verifyLabel,
                it.scrambled, it.processVersion, it.categoryLabel, it.categoryReason,
                it.failedStage)
        }.firstOrNull { it.key == sourceKey }
    }
    val cats: List<EngineData.MangaBrowseSource> = remember(catsBody) {
        if (catsBody.isBlank()) emptyList()
        else EngineData.exploreMangaSources(catsBody).filter { it.key == sourceKey }
    }
    val sel: ExploreCategory? = remember(cats, selUrl) {
        cats.firstOrNull()?.categories?.firstOrNull { it.url == selUrl }
    }

    /**
     * 重建该源的图片缓存：删除**旧处理版本**的图片（阅读缓存 + 下载目录都扫），
     * 下次阅读/下载时按当前算法重新拉取。书库条目、收藏、历史、其它源都不动。
     *
     * 为什么需要：块还原参数算错时写进缓存的是花图，而且会被一直复用；修好算法后
     * 不能要求用户清数据/重下整个书库（0.51.0 风险评估 §2.3 第 4、5 条）。
     */
    fun rebuildCache() {
        scope.launch {
            rebuildBusy = true
            rebuildMsg = ""
            val r = gateway.httpPost(ep.port,
                "/api/manga/${Uri.encode(sourceKey)}/rebuild-images", "")
            val o = runCatching { JSONObject(r.body) }.getOrNull()
            rebuildMsg = if (!r.ok) {
                "重建失败：HTTP ${r.code} " +
                    (o?.optString("error")?.take(100) ?: r.body.take(100))
            } else {
                val n = o?.optInt("removed_files") ?: 0
                val kb = ((o?.optLong("freed_bytes") ?: 0L) / 1024)
                val chs = o?.optJSONArray("chapters")?.length() ?: 0
                if (n == 0) "没有需要重建的图片缓存（都已是当前算法版本）"
                else "已重建 $chs 个章节：删除 $n 个旧图片文件（释放 ${kb}KB）；" +
                    "下次阅读会按新算法重新拉取"
            }
            rebuildBusy = false
            rebuildDialog = false
        }
    }

    LaunchedEffect(ep, sourceKey) {
        if (sourcesBody.isBlank()) {
            val r = gateway.httpText(ep.port, "/api/manga/sources")
            if (r.ok) sourcesBody = r.body
        }
        if (catsBody.isBlank()) {
            // 分类来自适配器自己声明：/api/explore/sources 的 manga 段
            val r = gateway.httpText(ep.port, "/api/explore/sources")
            if (r.ok) catsBody = r.body
        }
        loading = false
    }

    // 选中分类后：同屏切到结果（返回先回到本页的分类列表）
    if (sel != null) {
        val src = cats.firstOrNull()
        if (src != null) {
            Scaffold(topBar = {
                TopAppBar(
                    title = { Text("${src.name} · ${sel.title}", maxLines = 1,
                        overflow = TextOverflow.Ellipsis) },
                    navigationIcon = {
                        IconButton(onClick = { selUrl = "" }) {
                            Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                        }
                    },
                )
            }) { pad ->
                Box(Modifier.padding(pad).fillMaxSize()) {
                    MangaBrowseResultsScreen(gateway, ep, loader, src, sel, onOpen)
                }
            }
            return
        }
    }

    // 重建确认：先讲清楚"删什么、留什么、要重新联网拉取"
    if (rebuildDialog) {
        AlertDialog(
            onDismissRequest = { if (!rebuildBusy) rebuildDialog = false },
            title = { Text("重建图片缓存") },
            text = {
                Column(Modifier.testTag("manga_rebuild_dialog")) {
                    Text("将删除本源**旧还原算法处理过**的图片（在线阅读缓存 + 已下载章节的图片），" +
                        "下次阅读/下载时按当前算法重新拉取。",
                        style = MaterialTheme.typography.bodySmall)
                    Spacer(Modifier.height(6.dp))
                    Text("· 书库条目、收藏、阅读历史、下载记录都保留",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text("· 其它源的图片不受影响",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text("· 重建需要联网重新拉取受影响的图片",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            },
            confirmButton = {
                TextButton(onClick = { rebuildCache() }, enabled = !rebuildBusy,
                    modifier = Modifier.testTag("manga_rebuild_confirm")) {
                    Text(if (rebuildBusy) "重建中…" else "重建")
                }
            },
            dismissButton = {
                TextButton(onClick = { rebuildDialog = false },
                    modifier = Modifier.testTag("manga_rebuild_cancel")) { Text("取消") }
            },
        )
    }

    Scaffold(topBar = {
        TopAppBar(title = { Text(sourceName, maxLines = 1, overflow = TextOverflow.Ellipsis) },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                }
            })
    }) { pad ->
        Box(Modifier.padding(pad).fillMaxSize()) {
            when {
                loading -> Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
                else -> LazyColumn(Modifier.fillMaxSize().testTag("manga_source_page"),
                    contentPadding = PaddingValues(WnSpace.md),
                    verticalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                    item {
                        val c = card
                        WnHairlineCard {
                            Column(Modifier.padding(WnSpace.md)) {
                                Text(sourceName, style = MaterialTheme.typography.titleMedium,
                                    fontWeight = FontWeight.Bold)
                                Spacer(Modifier.height(WnSpace.xs))
                                Text(
                                    if (c == null) "该源未在漫画源清单里（可能未注册）"
                                    else "依赖判定：${c.reason.ifBlank { "满足" }}",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                                Text(
                                    "实测结论：${c?.verifyLabel ?: "无记录"}" +
                                        (c?.failedStage?.takeIf { it.isNotBlank() }
                                            ?.let { "（最早失败：$it）" } ?: ""),
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                                if (!c?.categoryLabel.isNullOrBlank()) {
                                    Text(
                                        "移动可用性：${c?.categoryLabel}" +
                                            (c?.categoryReason?.takeIf { it.isNotBlank() }
                                                ?.let { " · ${it.take(80)}" } ?: ""),
                                        style = MaterialTheme.typography.bodySmall,
                                        color = if (c?.categoryLabel?.contains("不支持") == true)
                                                    MaterialTheme.colorScheme.error
                                                else MaterialTheme.colorScheme.onSurfaceVariant,
                                        modifier = Modifier.testTag("manga_source_category"),
                                    )
                                }
                                Spacer(Modifier.height(6.dp))
                                Text(
                                    "「依赖判定」说明本机有没有它需要的运行环境；" +
                                        "「实测结论」来自逐源功能验证（搜索 → 目录 → 取一张真图）。" +
                                        "两者分开看：依赖满足不等于源站可用。",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                        }
                    }
                    item {
                        OutlinedButton(
                            onClick = { onOpen(Dest.MangaSearch(sourceKey = sourceKey)) },
                            modifier = Modifier.fillMaxWidth().testTag("manga_search_in_source"),
                        ) {
                            // 以前这里打开的是**不带源**的通用搜索（用户报"分源搜索失效"的主因之一）
                            Text("在「$sourceName」里搜漫画")
                        }
                    }
                    if (card?.scrambled == true) {
                        item {
                            Text("图片块还原：本源图片由服务器按块倒序还原后才可读" +
                                "（当前算法版本 v${card.processVersion}）。" +
                                "早期版本参数算错时，缓存里会留下错位的花图；" +
                                "下面可以只重建这些受影响章节，不动书库/收藏/历史。",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                            Spacer(Modifier.height(6.dp))
                            OutlinedButton(
                                onClick = { rebuildDialog = true },
                                enabled = !rebuildBusy,
                                modifier = Modifier.fillMaxWidth().testTag("manga_rebuild_cache"),
                            ) {
                                Text(if (rebuildBusy) "重建中…" else "重建图片缓存（修复块错乱）")
                            }
                        }
                    }
                    if (rebuildMsg.isNotBlank()) {
                        item {
                            Text(rebuildMsg,
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.primary,
                                modifier = Modifier.testTag("manga_rebuild_result"))
                        }
                    }
                    val catList = cats.firstOrNull()?.categories ?: emptyList()
                    item {
                        WnSectionHeader("可浏览的分类", count = catList.size)
                    }
                    if (catList.isEmpty()) {
                        item {
                            Text("该源未提供排行/分类接口——不生成假榜单。" +
                                "需要用关键词搜索时点上面的按钮。",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    } else {
                        items(catList, key = { it.url }) { c ->
                            OutlinedButton(
                                onClick = { selUrl = c.url },
                                modifier = Modifier.fillMaxWidth().testTag("src_cat_" + c.url),
                                contentPadding = PaddingValues(vertical = 6.dp),
                            ) { Text(c.title, maxLines = 1, overflow = TextOverflow.Ellipsis) }
                        }
                    }
                }
            }
        }
    }
}
