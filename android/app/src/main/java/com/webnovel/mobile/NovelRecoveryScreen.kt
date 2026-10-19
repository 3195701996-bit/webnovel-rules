package com.webnovel.mobile

import android.net.Uri
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.listSaver
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import kotlinx.coroutines.CancellationException
import org.json.JSONArray
import org.json.JSONObject

private data class NovelOriginal(val id: String, val label: String, val restorable: Boolean, val state: String)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun NovelRecoveryScreen(bookKey: String, port: Int, gateway: EngineGateway, onClose: () -> Unit, onChanged: () -> Unit) {
    val scope = rememberCoroutineScope()
    val base = "/api/books/${Uri.encode(bookKey)}/reclean"
    var rows by remember { mutableStateOf<List<NovelOriginal>>(emptyList()) }
    // Save identities only. A recreated dialog reloads eligibility and invalidates
    // its preview; rotation must never restore authority to perform a write.
    var selected by rememberSaveable(bookKey, stateSaver = listSaver<Set<String>, String>(
        save = { it.toList() }, restore = { it.toSet() },
    )) { mutableStateOf(emptySet()) }
    var busy by remember { mutableStateOf(true) }
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
            catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { rows = emptyList(); selected = emptySet(); status = error.message ?: "读取失败" }
            finally { busy = false }
        }
    }
    fun submitRestore() {
        if (busy || selected.isEmpty()) return
        val identities = selected.toList()
        val dry = !previewReady
        busy = true
        scope.launch {
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
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: Exception) {
                previewReady = false
                status = "操作未确认，请刷新存档后重试：${error.message}"
            } finally { busy = false }
        }
    }
    LaunchedEffect(bookKey) { refresh() }
    BackHandler { if (!busy) onClose() }
    Scaffold(
        modifier = Modifier.fillMaxSize().testTag("novel_recovery_screen"),
        topBar = { TopAppBar(title = { Text("恢复清洗前原文") }) },
        bottomBar = {
            Surface(tonalElevation = 2.dp) {
                Column {
                    Text(status, Modifier.padding(horizontal = 16.dp, vertical = 8.dp)
                        .testTag("novel_recovery_status"), style = MaterialTheme.typography.bodySmall)
                    Row(Modifier.fillMaxWidth().navigationBarsPadding().padding(horizontal = 16.dp, vertical = 8.dp),
                        horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        OutlinedButton(onClick = onClose, enabled = !busy,
                            modifier = Modifier.weight(1f).testTag("novel_recovery_close")) { Text("关闭") }
                        Button(enabled = !busy && selected.isNotEmpty(), onClick = { submitRestore() },
                            modifier = Modifier.weight(2f).testTag("novel_recovery_confirm")) {
                            Text(if (busy) "处理中…" else if (previewReady) "确认恢复" else "预览恢复（${selected.size}）")
                        }
                    }
                }
            }
        },
    ) { padding ->
        LazyColumn(Modifier.fillMaxSize().padding(padding).testTag("novel_recovery_list"),
            contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            item {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("恢复会带回该存档中的广告；仅替换仍等于清洗结果的章节，不覆盖后续下载或修改。", style = MaterialTheme.typography.bodySmall)
                    Text("本地存档 ${rows.size} 项 · 已选择 ${selected.size} 项", style = MaterialTheme.typography.labelLarge)
                    TextButton(onClick = { refresh() }, enabled = !busy) { Text("刷新存档") }
                    TextButton(onClick = {
                        val eligible = rows.filter { it.restorable }.take(500).map { it.id }.toSet()
                        selected = if (selected.containsAll(eligible)) emptySet() else eligible
                        previewReady = false
                    }, enabled = !busy && rows.any { it.restorable },
                        modifier = Modifier.testTag("novel_recovery_select_all")) { Text("全选 / 取消全选（最多500项）") }
                }
            }
            items(rows, key = { it.id }) { row ->
                Row(Modifier.fillMaxWidth().testTag("novel_recovery_pick_${row.id}")
                    .toggleable(value = row.id in selected, enabled = !busy && row.restorable, role = Role.Checkbox) { checked ->
                        if (!checked) selected = selected - row.id
                        else if (selected.size < 500) selected = selected + row.id
                        previewReady = false
                    }.padding(vertical = 4.dp)) {
                    Checkbox(checked = row.id in selected, onCheckedChange = null, enabled = !busy && row.restorable)
                    Column(Modifier.weight(1f).padding(start = 8.dp)) {
                        Text(row.label, style = MaterialTheme.typography.bodySmall)
                        Text(row.state, style = MaterialTheme.typography.labelSmall)
                    }
                }
            }
        }
    }
}
