package com.webnovel.mobile

import android.net.Uri
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import org.json.JSONObject

@Composable
fun NovelMetadataDialog(bookKey: String, port: Int, gateway: EngineGateway, onClose: () -> Unit, onChanged: () -> Unit) {
    val scope = rememberCoroutineScope()
    var busy by remember { mutableStateOf(false) }
    var preview by remember { mutableStateOf<JSONObject?>(null) }
    var status by remember { mutableStateOf("将联网读取原书源，仅修复书名与作者；目录、正文和阅读进度不变。") }
    var changes by remember { mutableStateOf(listOf<String>()) }
    AlertDialog(
        onDismissRequest = { if (!busy) onClose() },
        title = { Text("刷新书名与作者") },
        text = {
            LazyColumn(Modifier.heightIn(max = 360.dp)) {
                item { Text(status, Modifier.testTag("novel_metadata_status")) }
                items(changes.size) { index -> Text(changes[index]) }
            }
        },
        confirmButton = {
            TextButton(enabled = !busy, modifier = Modifier.testTag("novel_metadata_confirm"), onClick = {
                scope.launch {
                    busy = true
                    val approved = preview
                    try {
                        if (approved == null) {
                            status = "正在读取原书源…"; changes = emptyList()
                            val response = gateway.httpText(port, "/api/books/${Uri.encode(bookKey)}/metadata/preview")
                            require(response.ok) { "HTTP ${response.code} ${response.body.take(180)}" }
                            val data = JSONObject(response.body)
                            require(data.opt("ok") == true && data.opt("read_only") == true) { "预览响应无效" }
                            require(Regex("[0-9a-f]{64}").matches(data.getString("state_revision"))) { "预览版本无效" }
                            val proposed = data.getJSONObject("proposed")
                            require(proposed.opt("name") is String && proposed.getString("name").isNotBlank()) { "没有有效书名" }
                            require(proposed.keys().asSequence().all { it in setOf("name", "author") && proposed.opt(it) is String }) { "预览字段无效" }
                            val rows = data.getJSONArray("changes")
                            changes = (0 until rows.length()).map { index ->
                                val row = rows.getJSONObject(index)
                                val field = row.getString("field")
                                require(field in setOf("name", "author") && row.getString("after") == proposed.getString(field)) { "修改字段不一致" }
                                "${if (field == "name") "书名" else "作者"}：${row.optString("before")} → ${row.getString("after") }"
                            }
                            preview = if (changes.isNotEmpty()) data else null
                            status = if (changes.isEmpty()) "书名与作者无需修改。" else "请核对以下变化；确认后仅保存展示元数据，不更换书源或目录。"
                        } else {
                            val request = JSONObject().put("state_revision", approved.getString("state_revision"))
                                .put("metadata", approved.getJSONObject("proposed"))
                            val response = gateway.httpPost(port, "/api/books/${Uri.encode(bookKey)}/metadata", request.toString())
                            require(response.ok && JSONObject(response.body).opt("ok") == true) { "HTTP ${response.code} ${response.body.take(180)}" }
                            preview = null; onChanged(); onClose()
                        }
                    } catch (error: Exception) {
                        preview = null; changes = emptyList()
                        status = "操作未确认，请重新预览：${error.message}"
                    } finally { busy = false }
                }
            }) { Text(if (busy) "处理中…" else if (preview == null) "读取预览" else "确认修改") }
        },
        dismissButton = { TextButton(onClick = onClose, enabled = !busy, modifier = Modifier.testTag("novel_metadata_cancel")) { Text("取消") } },
    )
}
