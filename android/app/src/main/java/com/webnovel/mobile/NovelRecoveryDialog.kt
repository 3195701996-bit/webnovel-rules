package com.webnovel.mobile

import android.net.Uri
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject

private data class NovelOriginal(val id: String, val label: String, val restorable: Boolean, val state: String)

@Composable
fun NovelRecoveryDialog(bookKey: String, port: Int, gateway: EngineGateway, onClose: () -> Unit, onChanged: () -> Unit) {
    val scope = rememberCoroutineScope()
    val base = "/api/books/${Uri.encode(bookKey)}/reclean"
    var rows by remember { mutableStateOf<List<NovelOriginal>>(emptyList()) }
    var selected by remember { mutableStateOf(setOf<String>()) }
    var busy by remember { mutableStateOf(false) }
    var previewReady by remember { mutableStateOf(false) }
    var status by remember { mutableStateOf("正在读取原文存档…") }
    suspend fun load() {
        val response = gateway.httpText(port, "$base/recovery")
        require(response.ok) { "读取失败：HTTP ${response.code}" }
        val data = JSONObject(response.body)
        require(data.opt("ok") == true) { "存档响应无效" }
        val array = data.getJSONArray("items")
        require(data.getInt("count") == array.length()) { "存档数量不一致" }
        val parsed = (0 until array.length()).map { index ->
            val item = array.getJSONObject(index)
            val id = item.getString("id")
            require(Regex("[0-9a-f]{64}").matches(id)) { "存档身份无效" }
            val canRestore = item.opt("restorable") == true
            NovelOriginal(id, item.optString("label").ifBlank { "无法关联目录的存档" }, canRestore,
                if (canRestore) "可恢复" else if (item.opt("already_original") == true) "已经是原文" else "内容已变化或存档异常")
        }
        require(parsed.map { it.id }.distinct().size == parsed.size) { "存档身份重复" }
        rows = parsed
        selected = selected.intersect(parsed.filter { it.restorable }.map { it.id }.toSet())
        if (parsed.isEmpty()) status = "没有清洗前原文存档；旧版本清洗无法追溯恢复。"
    }
    fun refresh() {
        scope.launch {
            busy = true; previewReady = false
            try { load(); if (rows.isNotEmpty()) status = "请选择需要恢复的存档。" }
            catch (error: Exception) { rows = emptyList(); selected = emptySet(); status = error.message ?: "读取失败" }
            finally { busy = false }
        }
    }
    LaunchedEffect(bookKey) { refresh() }
    AlertDialog(
        onDismissRequest = { if (!busy) onClose() },
        title = { Text("恢复清洗前原文") },
        text = {
            LazyColumn(Modifier.heightIn(max = 360.dp).testTag("novel_recovery_list")) {
                item {
                Text("恢复会带回该存档中的广告；仅替换仍等于清洗结果的章节，不覆盖后续下载或修改。", style = MaterialTheme.typography.bodySmall)
                Text(status, Modifier.testTag("novel_recovery_status"), style = MaterialTheme.typography.bodySmall)
                TextButton(onClick = { refresh() }, enabled = !busy) { Text("刷新存档") }
                TextButton(onClick = {
                    val eligible = rows.filter { it.restorable }.take(500).map { it.id }.toSet()
                    selected = if (selected.containsAll(eligible)) emptySet() else eligible
                    previewReady = false
                }, enabled = !busy && rows.any { it.restorable }) { Text("全选 / 取消全选（最多500项）") }
                }
                    items(rows, key = { it.id }) { row ->
                        Row(Modifier.fillMaxWidth().testTag("novel_recovery_pick_${row.id}").clickable(enabled = !busy && row.restorable) {
                            if (row.id in selected) selected = selected - row.id
                            else if (selected.size < 500) selected = selected + row.id
                            previewReady = false
                        }.padding(vertical = 4.dp)) {
                            Checkbox(checked = row.id in selected, onCheckedChange = null, enabled = !busy && row.restorable)
                            Column(Modifier.weight(1f).padding(start = 8.dp)) {
                                Text(row.label, style = MaterialTheme.typography.bodySmall)
                                Text("${row.state} · 存档 ${row.id.take(8)}", style = MaterialTheme.typography.labelSmall)
                            }
                        }
                    }
            }
        },
        confirmButton = {
            TextButton(enabled = !busy && selected.isNotEmpty(), onClick = {
                val identities = selected.toList()
                val dry = !previewReady
                scope.launch {
                    busy = true
                    try {
                        val request = JSONObject().put("ids", JSONArray(identities)).put("dry_run", dry)
                        val response = gateway.httpPost(port, "/api/books/${Uri.encode(bookKey)}/reclean/restore", request.toString())
                        require(response.ok) { "请求失败：HTTP ${response.code}" }
                        val result = JSONObject(response.body)
                        require(result.opt("ok") == true && result.opt("dry_run") == dry) { "恢复响应无效" }
                        val results = result.getJSONArray("items")
                        val returnedIds = (0 until results.length()).map { results.getJSONObject(it).getString("id") }
                        require(returnedIds.size == identities.size && returnedIds.toSet() == identities.toSet()) { "恢复身份不一致" }
                        val failures = (0 until results.length()).map { results.getJSONObject(it) }.filter { it.opt("ok") != true }
                        previewReady = dry && failures.isEmpty()
                        status = if (failures.isNotEmpty()) "部分存档无法恢复：" + failures.take(3).joinToString("；") { it.optString("error") }
                        else if (dry) "预览通过，确认后恢复 ${identities.size} 项原文。"
                        else "已恢复 ${result.optInt("restored")} 项；" + if (result.optBoolean("txt_rebuilt")) "TXT已重建。" else "TXT未重建：${result.optString("export_error")}"
                        if (!dry) {
                            previewReady = false; selected = emptySet()
                            load(); onChanged()
                        }
                    } catch (error: Exception) {
                        previewReady = false
                        status = "操作未确认，请刷新存档后重试：${error.message}"
                    } finally { busy = false }
                }
            }, modifier = Modifier.testTag("novel_recovery_confirm")) {
                Text(if (busy) "处理中…" else if (previewReady) "确认恢复" else "预览恢复（${selected.size}）")
            }
        },
        dismissButton = { TextButton(onClick = onClose, enabled = !busy, modifier = Modifier.testTag("novel_recovery_close")) { Text("关闭") } },
    )
}
