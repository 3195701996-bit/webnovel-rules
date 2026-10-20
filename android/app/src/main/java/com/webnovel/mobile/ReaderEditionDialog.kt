package com.webnovel.mobile

import android.net.Uri
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import kotlinx.coroutines.CancellationException
import org.json.JSONObject

@Composable
fun ReaderEditionDialog(bookKey: String, port: Int, gateway: EngineGateway, onClose: () -> Unit, onExport: (String) -> Unit) {
    val scope = rememberCoroutineScope()
    // Preserve user options, but never persist a preview revision: recreating
    // the dialog must require a fresh read-only check before opening SAF.
    var clean by rememberSaveable(bookKey) { mutableStateOf(true) }
    var omit by rememberSaveable(bookKey) { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var report by remember { mutableStateOf<JSONObject?>(null) }
    var status by remember { mutableStateOf("先预览，再保存；不会修改原文缓存或原始TXT。") }
    val base = "/api/books/${Uri.encode(bookKey)}/txt"
    val options = "clean=${if (clean) 1 else 0}&omit_promotions=${if (omit) 1 else 0}"
    val preview = report
    val rows = buildList {
        preview?.optJSONArray("omitted")?.let { entries ->
            for (index in 0 until minOf(entries.length(), 100)) {
                val row = entries.optJSONObject(index) ?: continue
                add("排除目录第${row.optInt("source_index")}项 · ${row.optString("name")}")
            }
        }
        preview?.optJSONArray("chapters")?.let { entries ->
            for (index in 0 until minOf(entries.length(), 20)) {
                val row = entries.optJSONObject(index) ?: continue
                add("${row.optString("heading")}（原目录第${row.optInt("source_index")}项）")
            }
        }
    }
    AlertDialog(
        onDismissRequest = { if (!busy) onClose() },
        title = { Text("导出阅读版 TXT") },
        text = {
            LazyColumn(Modifier.heightIn(max = 360.dp).testTag("reader_edition_preview_list")) {
                item {
                Text("使用连续章节编号及标准标题，原始目录和阅读记录不变。", style = MaterialTheme.typography.bodySmall)
                Row {
                    Checkbox(clean, onCheckedChange = { clean = it; report = null; status = "选项已变更，请重新预览。" },
                        enabled = !busy, modifier = Modifier.testTag("reader_edition_clean"))
                    Text("去除正文中的已知广告", Modifier.padding(top = 12.dp), style = MaterialTheme.typography.bodySmall)
                }
                Row {
                    Checkbox(omit, onCheckedChange = { omit = it; report = null; status = "选项已变更，请重新预览。" },
                        enabled = !busy, modifier = Modifier.testTag("reader_edition_omit"))
                    Text("排除明确推广独立页（仅影响本次导出）", Modifier.weight(1f).padding(top = 12.dp), style = MaterialTheme.typography.bodySmall)
                }
                Text(status, Modifier.testTag("reader_edition_status"), style = MaterialTheme.typography.bodySmall)
                if (preview != null) {
                    Text("将导出 ${preview.optInt("exported_count")} 章 · 排除 ${preview.optInt("omitted_count")} 项 · 本地缺失 ${preview.optInt("missing_count")} 章",
                        style = MaterialTheme.typography.bodySmall)
                    if (preview.optInt("missing_count") > 0) Text("这是部分导出，缺失章节不会补下载。", style = MaterialTheme.typography.labelSmall)
                    Text("下方展示前100个排除项及前20个章节标题。", style = MaterialTheme.typography.labelSmall)
                }
                }
                items(rows) { Text(it, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(vertical = 4.dp)) }
            }
        },
        confirmButton = {
            TextButton(enabled = !busy, onClick = {
                if (preview != null) {
                    onExport("$base?layout=reader&$options&revision=${preview.getString("revision")}")
                } else scope.launch {
                    busy = true; status = "正在生成只读预览…"
                    try {
                        val response = gateway.httpText(port, "$base/preview?$options")
                        require(response.ok) { "HTTP ${response.code} ${response.body.take(120)}" }
                        val data = JSONObject(response.body)
                        require(data.opt("ok") == true && data.opt("clean") == clean && data.opt("omit_promotions") == omit) { "预览选项不一致" }
                        require(Regex("[0-9a-f]{64}").matches(data.getString("revision"))) { "预览版本无效" }
                        for (name in listOf("exported", "omitted", "missing")) {
                            val count = data.getInt("${name}_count")
                            val array = data.getJSONArray(if (name == "exported") "chapters" else name)
                            require(count >= 0 && count == array.length()) { "预览数量不一致" }
                        }
                        require(data.getInt("exported_count") > 0) { "没有本地正文章节" }
                        report = data; status = "预览已就绪，请核对后保存。正文若再变化，导出会拒绝并要求重新预览。"
                    } catch (cancelled: CancellationException) {
                        throw cancelled
                    } catch (error: Exception) {
                        report = null; status = "预览失败：${error.message}"
                    } finally { busy = false }
                }
            }, modifier = Modifier.testTag("reader_edition_confirm")) { Text(if (busy) "预览中…" else if (preview == null) "生成预览" else "保存阅读版") }
        },
        dismissButton = { TextButton(onClick = onClose, enabled = !busy, modifier = Modifier.testTag("reader_edition_cancel")) { Text("取消") } },
    )
}
