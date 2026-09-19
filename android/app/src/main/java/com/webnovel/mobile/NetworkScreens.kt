@file:OptIn(ExperimentalMaterial3Api::class)

package com.webnovel.mobile

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Button
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
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * **网络 → 代理（原生，可选）**：给出站流量指定一条网络路径。
 *
 * **先说清边界**：本 App 的设计前提是**独立运行**——装在手机上、自带引擎、随时可用，
 * 不连电脑、不连云端。代理**不是**运行前提：不配就是直连（默认），功能完全不受影响；
 * 它只服务"你自己本来就有代理"（机场/公司/局域网）的场景。界面文案必须让用户看出
 * 这一点，否则会以为"某些源必须连电脑才能用"——那是产品模型的改变，不是本功能。
 *
 * 真机诊断（2026-09-17）的结论是：API 三段很快（搜索 1222ms / 详情 478ms /
 * 章节 317ms），慢的是图片与部分源站域——拷贝/包子/nhentai 在部分网络下**直连
 * 被重置**，走代理即恢复。所以代理是"让内置源在受限网络下也能用"的手段之一，
 * 而非必需品。
 *
 * 三条纪律（与 server/net_api.py 同一口径）：
 *   1. 显示的是**真实生效值**（环境变量优先于配置文件），不是"用户填了什么"；
 *   2. 非法地址必须当场报错，绝不静默回落直连——"以为配上了其实没配"是最难排查的
 *      一类故障；
 *   3. 测试按钮把 直连 / 经代理 两列并排给出来并给结论：代理没扩展可达域时直说
 *      （多一跳只会更慢），不替代理说好话。
 */
@Composable
internal fun ProxySettingsScreen(
    gateway: EngineGateway,
    ep: EngineEndpoint,
    onBack: () -> Unit,
) {
    val scope = rememberCoroutineScope()
    var loading by remember { mutableStateOf(true) }
    var addr by remember { mutableStateOf("") }          // 主机:端口（不含 scheme）
    var scheme by remember { mutableStateOf("http") }    // http | socks5
    var stateText by remember { mutableStateOf("读取中…") }
    var msg by remember { mutableStateOf<String?>(null) }
    var testText by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var testing by remember { mutableStateOf(false) }

    // 服务端返回的代理串 → 拆分到"类型 + 地址"两个控件；用户粘贴完整 URL 也能对上
    fun applyState(o: JSONObject?) {
        val proxy = o?.optString("proxy").orEmpty()
        val src = o?.optString("source").orEmpty()
        if (proxy.contains("://")) {
            val head = proxy.substringBefore("://")
            val tail = proxy.substringAfter("://")
            scheme = if (head.startsWith("socks")) "socks5" else "http"
            addr = tail
        } else {
            addr = ""
        }
        // 代理配了但连不上（电脑关机/换网络/地址写错）：必须**当面说清**。
        // 真机事故：用户按说明把代理指向电脑，之后电脑关机 → 所有源都超时，
        // 而界面只显示“经代理”，看不出原因。现在引擎会自动临时直连并在这里告知。
        val health = o?.optJSONObject("health")
        val fellBack = health != null && health.optBoolean("fell_back")
        stateText = when {
            fellBack -> "⚠ 你配置的代理当前**连不上**，已临时改用直连（地址仍保留）：\n" +
                (health?.optString("err").orEmpty().take(120)) +
                "\n要真正走代理请确认：代理已开启、手机与它在同一网络、地址端口没写错。"
            proxy.isBlank() -> "当前：直连（未配置代理 —— 这是默认、也是正常的运行方式，" +
                "App 独立运行，不需要电脑或云端）"
            src == "env" -> "当前：经代理 $proxy（来源：环境变量，优先级高于这里）"
            else -> "当前：经代理 $proxy（保存在应用私有目录，重启引擎仍生效）"
        }
        o?.optString("config_path")?.takeIf { it.isNotBlank() }?.let {
            stateText += "\n配置文件：$it"
        }
        // 小说侧是**另一套实现**（指南 P1-2）：上面那个开关只对漫画/图片通道生效，
        // 小说书源还可能走 环境变量 WR_PROXY 或 data/proxies.txt 代理池 —— 必须分开说清，
        // 否则用户看到"直连"会以为小说也直连（或反之），无从自查。
        o?.optJSONObject("novel")?.let { nv ->
            stateText += "\n\n小说书源代理（与上面**不是同一个开关**）：" + when (nv.optString("mode")) {
                "env" -> "经环境变量 WR_PROXY = ${nv.optString("env_proxy")}（优先级高于代理池）"
                "pool" -> "使用代理池 data/proxies.txt（${nv.optInt("pool_size")} 条；" +
                    "仅对**直连失败过**的域名启用，当前快代理 ${nv.optInt("good_count")} 条）"
                else -> "直连（无 WR_PROXY、代理池为空）"
            }
        }
        loading = false
    }

    suspend fun refresh() {
        val r = gateway.httpText(ep.port, "/api/net/proxy")
        if (!r.ok) {
            stateText = "读取代理状态失败：HTTP ${r.code}"
            loading = false
            return
        }
        applyState(runCatching { JSONObject(r.body).optJSONObject("data") }.getOrNull())
    }

    /** 控件里的值 → 提交给服务端的代理串（空 = 清除代理） */
    fun candidate(): String {
        val a = addr.trim()
        if (a.isEmpty()) return ""
        if (a.contains("://")) return a
        return "$scheme://$a"
    }

    LaunchedEffect(ep) { refresh() }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text("网络代理", maxLines = 1) },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                }
            },
            actions = {
                TextButton(onClick = { scope.launch { refresh() } }, enabled = !busy) {
                    Text("刷新")
                }
            },
        )
    }) { pad ->
    Column(
        Modifier.padding(pad).fillMaxSize().verticalScroll(rememberScrollState()).padding(WnSpace.md),
        verticalArrangement = Arrangement.spacedBy(WnSpace.sm),
    ) {
        Text(stateText, style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.testTag("proxy_state"))

        Text("类型", style = MaterialTheme.typography.labelMedium)
        Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
            FilterChip(
                selected = scheme == "http",
                onClick = { scheme = "http" },
                label = { Text("HTTP") },
                shape = WnChipShape,
                modifier = Modifier.testTag("proxy_scheme_http"),
            )
            FilterChip(
                selected = scheme == "socks5",
                onClick = { scheme = "socks5" },
                label = { Text("SOCKS5") },
                shape = WnChipShape,
                modifier = Modifier.testTag("proxy_scheme_socks5"),
            )
        }

        OutlinedTextField(
            value = addr,
            onValueChange = { addr = it },
            label = { Text("地址（主机:端口）") },
            placeholder = { Text("例如 127.0.0.1:7897") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth().testTag("proxy_addr"),
        )
        Text(
            "支持 HTTP / SOCKS5（含 socks5h）；带用户名密码就写 用户名:密码@主机:端口。" +
                "保存后立即生效（会重置各通道连接，下次请求就走新路径），不需要重启引擎。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.outline,
        )
        Text(
            "没有自己的代理就别配：默认直连就是这个 App 的正常运行方式，" +
                "它不需要电脑或云端。只有你本来就有代理（机场/公司/局域网）时才在这里填。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.primary,
            modifier = Modifier.testTag("proxy_optional_note"),
        )

        Row(horizontalArrangement = Arrangement.spacedBy(WnSpace.sm)) {
            Button(
                onClick = {
                    busy = true; msg = "正在保存…"
                    scope.launch {
                        val body = JSONObject().put("proxy", candidate()).toString()
                        val r = gateway.httpPost(ep.port, "/api/net/proxy", body)
                        val o = runCatching { JSONObject(r.body) }.getOrNull()
                        msg = if (r.ok && o?.optBoolean("ok") == true) {
                            o.optString("note").ifBlank { "已保存" }
                        } else {
                            // 服务端明确拒绝（写法不合法）：把原因原样显示，不含糊过去
                            "保存失败：HTTP ${r.code} · " +
                                (o?.optString("error")?.take(120) ?: r.body.take(120))
                        }
                        applyState(o?.optJSONObject("data"))
                        busy = false
                    }
                },
                enabled = !busy && !loading,
                modifier = Modifier.testTag("proxy_save"),
            ) { Text(if (busy) "保存中…" else "保存并生效") }
            OutlinedButton(
                onClick = {
                    busy = true
                    scope.launch {
                        val body = JSONObject().put("proxy", "").toString()
                        val r = gateway.httpPost(ep.port, "/api/net/proxy", body)
                        msg = if (r.ok) "已清除代理，恢复直连" else "清除失败：HTTP ${r.code}"
                        applyState(runCatching {
                            JSONObject(r.body).optJSONObject("data")
                        }.getOrNull())
                        busy = false
                    }
                },
                enabled = !busy && !loading,
                modifier = Modifier.testTag("proxy_clear"),
            ) { Text("清除代理") }
        }

        OutlinedButton(
            onClick = {
                testing = true; testText = "正在探测（最多约 20 秒）…"
                scope.launch {
                    // 用**当前控件里的候选值**去测：用户可以先测再决定要不要保存
                    val body = JSONObject().put("proxy", candidate()).toString()
                    val r = withContext(Dispatchers.IO) {
                        gateway.httpPost(ep.port, "/api/net/proxy/test", body)
                    }
                    testText = if (r.ok) {
                        runCatching { JSONObject(r.body).optString("text") }
                            .getOrNull()?.takeIf { it.isNotBlank() } ?: r.body.take(800)
                    } else {
                        "探测失败：HTTP ${r.code} · ${r.body.take(160)}"
                    }
                    testing = false
                }
            },
            enabled = !testing && !busy && !loading,
            modifier = Modifier.testTag("proxy_test"),
        ) { Text(if (testing) "探测中…" else "测试连通性（含直连对照）") }
        testText?.let {
            Text(it, style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
                modifier = Modifier.testTag("proxy_test_result"))
        }
        msg?.let {
            Text(it, style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.primary,
                modifier = Modifier.testTag("proxy_msg"))
        }

        Spacer(Modifier.height(WnSpace.xs))
        Text(
            "边界说明：代理只是一条网络路径——它不绕过任何站点的风控（例如拷贝漫画的 " +
                "210「破解版」标记与代理无关），经代理时本机也不再对目标做 IP 绑定。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.outline,
        )
    }
    }
}
