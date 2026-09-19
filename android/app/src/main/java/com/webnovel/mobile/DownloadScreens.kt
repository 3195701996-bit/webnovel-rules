@file:OptIn(ExperimentalMaterial3Api::class)

package com.webnovel.mobile

import androidx.compose.foundation.background
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
import androidx.compose.material.icons.filled.Download
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * 下载页（原生）：队列来自 GET /api/tasks（小说任务与漫画下载在同一列表里）。
 *
 * - 逐条显示状态、进度、速度与预计时间（数据来自服务端，不自己估算）。
 * - 逐条暂停/继续/停止；漫画用专用接口（source+cid），小说用 /api/tasks/<id>/...
 * - 全局"全部暂停/全部继续"用批量接口，返回的条数如实显示（不假装操作了 N 个）。
 * - 有任务在跑时每 3 秒自动刷新；停止刷新不会停任务（任务在引擎侧）。
 * - 队列为空时明确说明，而不是空白。
 */
@Composable
internal fun DownloadsScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    onOpen: (Dest) -> Unit,
) {
    var tasks by remember { mutableStateOf<List<EngineData.TaskItem>>(emptyList()) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var actionMsg by remember { mutableStateOf<String?>(null) }
    var libUpdate by remember { mutableStateOf<MangaLibraryUpdate?>(null) }
    var checking by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    suspend fun refresh() {
        val r = gateway.httpText(ep.port, "/api/tasks")
        if (!r.ok) {
            error = "读取任务失败：HTTP ${r.code}"
        } else {
            tasks = EngineData.tasks(r.body)
            error = null
        }
        loading = false
    }

    LaunchedEffect(ep) {
        refresh()
        // 有任务在跑时轮询；停下来的任务不再轮询，界面显示的是服务端状态
        while (true) {
            delay(3000)
            if (tasks.any { it.running }) refresh()
        }
    }

    val running = tasks.filter { it.running }
    val paused = tasks.filter { it.status == "paused" || it.status == "stopped" }
    val finished = tasks.filter { !it.running && it.status != "paused" && it.status != "stopped" }

    LazyColumn(
        Modifier.fillMaxSize().testTag("downloads_list"),
        contentPadding = PaddingValues(WnSpace.md),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                Button(
                    onClick = {
                        scope.launch {
                            val r = gateway.httpPost(ep.port, "/api/manga/download/pause-all")
                            val n = runCatching {
                                org.json.JSONObject(r.body).optInt("paused")
                            }.getOrDefault(0)
                            actionMsg = if (r.ok) "已请求暂停 $n 个漫画下载" else "全部暂停失败：HTTP ${r.code}"
                            refresh()
                        }
                    },
                    modifier = Modifier.weight(1f),
                ) { Text("全部暂停") }
                OutlinedButton(
                    onClick = {
                        scope.launch {
                            val r = gateway.httpPost(ep.port, "/api/manga/download/resume-all")
                            val n = runCatching {
                                org.json.JSONObject(r.body).optInt("resumed")
                            }.getOrDefault(0)
                            actionMsg = if (r.ok) "已恢复 $n 个漫画下载" else "全部继续失败：HTTP ${r.code}"
                            refresh()
                        }
                    },
                    modifier = Modifier.weight(1f),
                ) { Text("全部继续") }
            }
            actionMsg?.let {
                Spacer(Modifier.height(6.dp))
                Text(it, style = MaterialTheme.typography.labelMedium,
                     color = MaterialTheme.colorScheme.primary)
            }
            Spacer(Modifier.height(WnSpace.sm))
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                OutlinedButton(
                    onClick = {
                        scope.launch {
                            checking = true; actionMsg = "正在检查书库更新…"
                            val start = gateway.httpPost(ep.port,
                                "/api/manga/library/check-updates")
                            if (start.code == 409) {
                                actionMsg = "已有一轮检查在进行中，跟随它的进度"
                            } else if (!start.ok) {
                                actionMsg = "启动检查失败：HTTP ${start.code}"
                                checking = false
                            }
                            var u = MangaLibraryUpdate(false, 0, 0, emptyList())
                            for (i in 0 until 90) {
                                kotlinx.coroutines.delay(2000)
                                val st = gateway.httpText(ep.port,
                                    "/api/manga/library/check-updates/status")
                                val o = runCatching {
                                    org.json.JSONObject(st.body)
                                }.getOrNull() ?: continue
                                u = EngineData.mangaLibraryUpdate(st.body)
                                if (!u.running) break
                                actionMsg = "检查书库更新：${u.label}"
                            }
                            libUpdate = u
                            actionMsg = u.label
                            checking = false
                        }
                    },
                    enabled = !checking,
                    modifier = Modifier.weight(1f).testTag("check_library_update"),
                ) { Text(if (checking) "检查中…" else "检查书库更新") }
                OutlinedButton(
                    onClick = {
                        scope.launch {
                            val r = gateway.httpPost(ep.port, "/api/manga/check-updates-download")
                            val n = runCatching {
                                org.json.JSONObject(r.body).optInt("started")
                            }.getOrDefault(0)
                            actionMsg = if (r.ok) "已创建更新下载任务：$n 部（见上方队列）"
                                        else "下载更新失败：HTTP ${r.code} ${r.body.take(80)}"
                            refresh()
                        }
                    },
                    enabled = !checking && (libUpdate?.withUpdate?.isNotEmpty() == true),
                    modifier = Modifier.weight(1f),
                ) { Text("下载更新（${libUpdate?.withUpdate?.size ?: 0} 部）") }
            }
            libUpdate?.let { u ->
                Spacer(Modifier.height(WnSpace.xs))
                Text(u.label, style = MaterialTheme.typography.labelMedium,
                     color = MaterialTheme.colorScheme.primary)
                u.withUpdate.take(3).forEach { (title, c) ->
                    Text("· $title：${c.label}", style = MaterialTheme.typography.labelSmall,
                         color = MaterialTheme.colorScheme.outline,
                         maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
            }
            Spacer(Modifier.height(WnSpace.xs))
            Text(
                "任务由本机引擎执行：退出界面或锁屏不会停止正在进行的任务；" +
                    "系统限制后台时会保存进度，回到应用后继续。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }

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
            tasks.isEmpty() -> item {
                Box(Modifier.fillMaxWidth().fillParentMaxHeight(0.72f)) {
                    WnEmptyState(
                        icon = Icons.Filled.Download,
                        title = "当前没有下载任务",
                        body = "小说在「书架 → 详情」里开始下载，漫画在「详情 → 下载本话」开始下载。",
                    )
                }
            }
        }

        if (running.isNotEmpty()) {
            item { WnSectionHeader("进行中", count = running.size) }
            items(running, key = { it.id }) { t ->
                TaskCard(t, busy = false,
                    onPause = {
                        scope.launch { doAction(gateway, ep, t, "pause").let { actionMsg = it }; refresh() }
                    },
                    onResume = {
                        scope.launch { doAction(gateway, ep, t, "resume").let { actionMsg = it }; refresh() }
                    },
                    onStop = {
                        scope.launch { doAction(gateway, ep, t, "stop").let { actionMsg = it }; refresh() }
                    },
                    onDelete = {
                        scope.launch { doDelete(gateway, ep, t).let { actionMsg = it }; refresh() }
                    },
                )
            }
        }
        if (paused.isNotEmpty()) {
            item { WnSectionHeader("已暂停 / 已停止", count = paused.size) }
            items(paused, key = { it.id }) { t ->
                TaskCard(t, busy = false,
                    onPause = null,
                    onResume = {
                        scope.launch { doAction(gateway, ep, t, "resume").let { actionMsg = it }; refresh() }
                    },
                    onStop = null,
                    onDelete = {
                        scope.launch { doDelete(gateway, ep, t).let { actionMsg = it }; refresh() }
                    },
                )
            }
        }
        if (finished.isNotEmpty()) {
            item { WnSectionHeader("已结束", count = finished.size) }
            items(finished.take(20), key = { it.id }) { t ->
                TaskCard(t, busy = false, onPause = null, onResume = null, onStop = null,
                    onDelete = {
                        scope.launch { doDelete(gateway, ep, t).let { actionMsg = it }; refresh() }
                    })
            }
        }

        item {
            Spacer(Modifier.height(6.dp))
            TextButton(onClick = { scope.launch { refresh() } }) { Text("刷新") }
            OutlinedButton(onClick = { onOpen(Dest.Web("/manga_download", "漫画下载")) }) {
                Text("选择章节下载（完整网页界面）")
            }
            Spacer(Modifier.height(WnSpace.sm))
            Text(
                "小说任务与导出的完整操作仍在网页界面（/tasks_page）；" +
                    "此页只做队列状态与控制，不复制一套规则。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }
    }
}

private suspend fun doAction(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    t: EngineData.TaskItem,
    action: String,
): String {
    // 漫画任务用漫画专用接口（按 source+cid），小说任务用 /api/tasks/<id>/<action>
    if (t.isManga) {
        if (action != "pause" && action != "resume") {
            return "漫画任务不支持该操作（请在网页下载页取消）"
        }
        val path = "/api/manga/download/$action?source=${android.net.Uri.encode(t.mangaSource)}" +
            "&cid=${android.net.Uri.encode(t.mangaComicId)}"
        val r = gateway.httpPost(ep.port, path)
        return if (r.ok) "已请求${if (action == "pause") "暂停" else "继续"}：${t.title}"
        else "操作失败：HTTP ${r.code}"
    }
    val r = gateway.httpPost(ep.port, "/api/tasks/${android.net.Uri.encode(t.id)}/$action")
    return if (r.ok) "已请求${when (action) {
        "pause" -> "暂停"; "resume" -> "继续"; else -> "停止"
    }}：${t.title}" else "操作失败：HTTP ${r.code} ${r.body.take(80)}"
}

/**
 * 删除任务记录（漫画走 /api/tasks/manga_<source:cid>，小说走 /api/tasks/<id>）。
 * 说明：**只删任务记录，不删已下载的图片/章节缓存**——避免误删用户已下载内容。
 */
private suspend fun doDelete(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    t: EngineData.TaskItem,
): String {
    val path = if (t.isManga) "/api/tasks/manga_${android.net.Uri.encode(t.mangaKey)}"
               else "/api/tasks/${android.net.Uri.encode(t.id)}"
    val r = gateway.httpDelete(ep.port, path)
    return if (r.ok) "已删除任务记录：${t.title}（已下载内容保留）"
           else "删除失败：HTTP ${r.code} ${r.body.take(80)}"
}

@Composable
private fun TaskCard(
    t: EngineData.TaskItem,
    busy: Boolean,
    onPause: (() -> Unit)?,
    onResume: (() -> Unit)?,
    onStop: (() -> Unit)?,
    onDelete: (() -> Unit)? = null,
) {
    WnHairlineCard {
        Column(Modifier.padding(WnSpace.md)) {
            Text(t.title.ifBlank { t.id }, style = MaterialTheme.typography.bodyMedium,
                 maxLines = 2, overflow = TextOverflow.Ellipsis)
            Spacer(Modifier.height(WnSpace.xs))
            Text(
                t.label + t.speedLabel,
                style = MaterialTheme.typography.labelMedium,
                // 状态色三态：下载中=accent、完成=ok、失败/停止=danger；暂停用灰
                color = when {
                    t.status == "error" || t.status == "stopped" -> WnColors.danger
                    t.running -> WnColors.accent
                    t.status == "paused" -> WnColors.inkDim
                    else -> WnColors.ok
                },
            )
            if (t.total > 0) {
                Spacer(Modifier.height(6.dp))
                LinearProgressIndicator(
                    progress = { t.percent / 100f },
                    modifier = Modifier.fillMaxWidth().height(6.dp)
                        .clip(WnChipShape)
                        .background(MaterialTheme.colorScheme.surfaceVariant),
                )
            }
            if (t.current.isNotBlank()) {
                Spacer(Modifier.height(WnSpace.xs))
                Text(t.current, style = MaterialTheme.typography.labelSmall,
                     color = MaterialTheme.colorScheme.outline, maxLines = 1,
                     overflow = TextOverflow.Ellipsis)
            }
            // P1-1：为什么停了 —— 服务端给的结构化原因（用户停的 / 系统停的服务 /
            // 进程被结束 / 任务出错），不显示"未知状态"让用户猜
            if (t.showStopReason) {
                Spacer(Modifier.height(WnSpace.xs))
                Text(
                    t.stopReason,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                    modifier = Modifier.testTag("task_stop_reason"),
                )
                if (t.checkpointLabel.isNotBlank()) {
                    Spacer(Modifier.height(2.dp))
                    Text(
                        "${t.checkpointLabel}（继续会从断点接着下）",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                        modifier = Modifier.testTag("task_checkpoint"),
                    )
                }
            }
            if (onPause != null || onResume != null || onStop != null || onDelete != null) {
                Spacer(Modifier.height(6.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
                    if (onPause != null && t.running) {
                        OutlinedButton(onClick = onPause, enabled = !busy) { Text("暂停") }
                    }
                    if (onResume != null && !t.running && t.resumable) {
                        OutlinedButton(onClick = onResume, enabled = !busy,
                            modifier = Modifier.testTag("task_resume")) { Text("继续") }
                    }
                    if (onStop != null && t.running && !t.isManga) {
                        OutlinedButton(onClick = onStop, enabled = !busy) { Text("停止") }
                    }
                    if (onDelete != null) {
                        // 漫画：运行中=取消下载（服务端会标记停止并移除记录）；
                        // 小说：删除任务记录（同时会请求停止）。都不删已下载内容。
                        OutlinedButton(onClick = onDelete, enabled = !busy) {
                            Text(if (t.isManga) { if (t.running) "取消下载" else "删除记录" } else "删除记录")
                        }
                    }
                }
            }
        }
    }
}
