package com.webnovel.mobile

import android.net.Uri
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject

/** Management uses disk IDs, never the readable local catalog or online names. */
@Composable
internal fun LocalMediaManagerDialog(gateway: EngineGateway, ep: EngineEndpoint,
    source: String, comicId: String, onDismiss: () -> Unit, onChanged: () -> Unit) {
    val scope = rememberCoroutineScope()
    var inventory by remember(source, comicId) { mutableStateOf<LocalMediaInventory?>(null) }
    var selected by remember(source, comicId) { mutableStateOf(emptySet<String>()) }
    var loading by remember { mutableStateOf(true) }
    var busy by remember { mutableStateOf(false) }
    var message by remember { mutableStateOf("") }
    var loadError by remember { mutableStateOf(false) }
    var confirm by remember { mutableStateOf(false) }
    var query by remember(source, comicId) { mutableStateOf("") }
    val keyboard = LocalSoftwareKeyboardController.current
    val base = "/api/manga/${Uri.encode(source)}/${Uri.encode(comicId)}"
    suspend fun refresh() {
        val recoveringFromReadError = loadError
        loading = true
        loadError = false
        try {
            val response = gateway.httpText(ep.port, "$base/local-media")
            val data = if (response.ok) EngineData.mangaLocalMedia(response.body) else null
            if (data == null || data.source != source || data.comicId != comicId) {
                loadError = true
                message = "本地清单读取失败，请重试；不会使用在线目录代替。"
            } else {
                inventory = data
                selected = selected.intersect(data.media.map { it.id }.toSet())
                if (recoveringFromReadError) message = ""
            }
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            loadError = true
            message = "本地清单读取失败，请重试。"
        } finally { loading = false }
    }
    LaunchedEffect(source, comicId) { refresh() }
    val rows = inventory?.media.orEmpty()
    val visible = remember(rows, query) { filterLocalMedia(rows, query) }
    val visibleIds = remember(visible) { visible.map { it.id }.toSet() }
    val hiddenSelected = selected.count { it !in visibleIds }
    val hints = remember(rows) { mangaChapterIdentityHints(rows.map { MangaChapter(it.id, it.name, "") }) }
    val enabled = !loading && !busy && !loadError
    AlertDialog(onDismissRequest = { if (!busy) onDismiss() },
        title = { Text("本地缓存管理") },
        text = {
            Column(Modifier.testTag("local_media_manager")) {
                Text("仅清理本机完整或部分缓存；收藏、历史和未选择内容保留。",
                    style = MaterialTheme.typography.bodySmall)
                if (loading) Text("正在读取本地清单…")
                if (loadError) TextButton(onClick = { scope.launch { refresh() } }, enabled = !busy,
                    modifier = Modifier.testTag("local_media_retry")) { Text("重试读取") }
                if (inventory != null && !loading) {
                    Text("${rows.size} 个单元 · 部分缓存 ${rows.count { it.partial }} 个",
                        style = MaterialTheme.typography.bodySmall)
                    if (rows.size > 10 || query.isNotEmpty()) OutlinedTextField(value = query,
                        onValueChange = { query = it }, singleLine = true, enabled = enabled,
                        label = { Text("筛选标题或 ID") },
                        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                        keyboardActions = KeyboardActions(onDone = { keyboard?.hide() }),
                        modifier = Modifier.fillMaxWidth().testTag("local_media_search"))
                    if (query.isNotBlank()) Text("匹配 ${visible.size} 项 · 筛选外已选 $hiddenSelected 项",
                        style = MaterialTheme.typography.bodySmall)
                    TextButton(enabled = enabled && visible.isNotEmpty(), onClick = {
                        selected = toggleLocalMediaSelection(selected, visible)
                    }, modifier = Modifier.testTag("local_media_select_all")) {
                        Text(if (visible.isNotEmpty() && selected.containsAll(visibleIds)) "取消全选"
                            else if (query.isNotBlank()) "全选筛选结果" else "全选本地内容")
                    }
                    if (rows.isEmpty()) Text("没有可清理的本地内容")
                    else if (visible.isEmpty()) Text("没有匹配的本地内容，可修改筛选词")
                    LazyColumn(Modifier.fillMaxWidth().heightIn(max = 280.dp).testTag("local_media_list")) {
                        items(visible, key = { it.id }) { row ->
                            Row(Modifier.fillMaxWidth().clickable(enabled = enabled) {
                                selected = if (row.id in selected) selected - row.id else selected + row.id
                            }.padding(vertical = 4.dp)) {
                                Checkbox(checked = row.id in selected, enabled = enabled,
                                    onCheckedChange = { checked ->
                                        selected = if (checked) selected + row.id else selected - row.id
                                    }, modifier = Modifier.testTag("local_media_pick_${row.id}"))
                                Column(Modifier.weight(1f).padding(top = 8.dp)) {
                                    Text(row.name)
                                    Text(if (row.partial) "部分缓存" else "完整缓存",
                                        style = MaterialTheme.typography.labelSmall)
                                    hints[row.id]?.let { Text("身份 $it", style = MaterialTheme.typography.labelSmall) }
                                }
                            }
                        }
                    }
                }
                if (message.isNotBlank()) Text(message, style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.testTag("local_media_status"))
            }
        },
        confirmButton = {
            TextButton(enabled = enabled && selected.isNotEmpty(), onClick = { confirm = true },
                modifier = Modifier.testTag("local_media_delete")) {
                Text(if (busy) "删除中…" else "删除选中（${selected.size}）")
            }
        },
        dismissButton = {
            TextButton(enabled = !busy, onClick = onDismiss,
                modifier = Modifier.testTag("local_media_close")) { Text("关闭") }
        })
    if (confirm) AlertDialog(onDismissRequest = { confirm = false },
        title = { Text("删除选中的本地缓存？") },
        text = { Text("将删除 ${selected.size} 个单元的本地图片。" +
            (if (hiddenSelected > 0) "其中 $hiddenSelected 项位于当前筛选之外。" else "") +
            "文件删除后不可恢复。收藏与阅读记录保留；仅按选中 ID 删除。") },
        confirmButton = {
            TextButton(onClick = {
                confirm = false
                busy = true
                val ids = selected.toList()
                scope.launch {
                    try {
                        val body = JSONObject().put("chapter_ids", JSONArray(ids))
                            .put("identity_mode", "disk").toString()
                        val response = gateway.httpPost(ep.port, mangaPath(source, comicId, "/chapters/delete"), body)
                        val result = runCatching { JSONObject(response.body) }.getOrNull()
                        val succeeded = response.ok && result?.opt("ok") == true && result.optInt("missing", -1) == 0
                        message = if (succeeded) "已删除 ${result!!.optInt("deleted")} 项本地内容"
                            else "删除未完成：" +
                                (result?.optString("error")?.takeIf { it.isNotBlank() }?.take(120)
                                    ?: if (response.ok) "部分单元已不存在，请核对清单" else "HTTP ${response.code}") +
                                "。已保留仍在本地的选择，可重试；部分文件可能已删除。"
                        if (succeeded) selected = emptySet()
                        onChanged()
                        refresh()
                    } catch (cancelled: CancellationException) {
                        throw cancelled
                    } catch (_: Exception) {
                        message = "删除未完成，请刷新本地清单后重试。"
                        onChanged()
                        refresh()
                    } finally { busy = false }
                }
            }, modifier = Modifier.testTag("local_media_delete_confirm")) { Text("删除") }
        }, dismissButton = { TextButton(onClick = { confirm = false }) { Text("取消") } })
}
