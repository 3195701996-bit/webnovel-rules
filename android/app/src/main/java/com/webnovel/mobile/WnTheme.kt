package com.webnovel.mobile

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.Check
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.ImageLoader
import coil.compose.AsyncImage
import coil.request.ImageRequest
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * 暖墨色暗色设计体系：单色暖灰底 + 唯一琥珀 accent，扁平表面 + 发丝线分隔，
 * 不用投影、不用玻璃拟态、不用渐变装饰。
 */
object WnColors {
    val bg = Color(0xFF23221F)
    val surface = Color(0xFF2C2B27)
    val surfaceVar = Color(0xFF35332F)
    val ink = Color(0xFFF5F5F0)
    val inkDim = Color(0xFF8A8A80)
    val line = Color(0xFF403E38)
    val accent = Color(0xFFE0A458)
    val onAccent = Color(0xFF241A0E)
    val danger = Color(0xFFE06C6C)
    val ok = Color(0xFF7FBF8E)
}

/** 全局换肤：现存 MaterialTheme.colorScheme.* 引用经此映射自动落到新色板 */
fun wnColorScheme(): ColorScheme = darkColorScheme(
    primary = WnColors.accent,
    onPrimary = WnColors.onAccent,
    primaryContainer = WnColors.surfaceVar,
    onPrimaryContainer = WnColors.accent,
    secondary = WnColors.accent,
    onSecondary = WnColors.onAccent,
    secondaryContainer = WnColors.surfaceVar,
    onSecondaryContainer = WnColors.ink,
    tertiary = WnColors.ok,
    onTertiary = WnColors.onAccent,
    background = WnColors.bg,
    onBackground = WnColors.ink,
    surface = WnColors.surface,
    onSurface = WnColors.ink,
    surfaceVariant = WnColors.surfaceVar,
    onSurfaceVariant = WnColors.inkDim,
    surfaceTint = WnColors.accent,
    inverseSurface = WnColors.ink,
    inverseOnSurface = WnColors.bg,
    inversePrimary = WnColors.accent,
    error = WnColors.danger,
    onError = WnColors.onAccent,
    errorContainer = WnColors.surfaceVar,
    onErrorContainer = WnColors.danger,
    outline = WnColors.line,
    outlineVariant = WnColors.surfaceVar,
    scrim = Color(0xCC141310),
    surfaceBright = WnColors.surfaceVar,
    surfaceDim = WnColors.bg,
    surfaceContainerLowest = WnColors.bg,
    surfaceContainerLow = WnColors.bg,
    surfaceContainer = WnColors.surface,
    surfaceContainerHigh = WnColors.surfaceVar,
    surfaceContainerHighest = WnColors.surfaceVar,
)

/** 圆角：卡片 14dp、小元素（chip/进度条）10dp、按钮 pill */
val WnCardShape = RoundedCornerShape(14.dp)
val WnChipShape = RoundedCornerShape(10.dp)
val WnPillShape = RoundedCornerShape(50)

/** 统一间距刻度 */
object WnSpace {
    val xs = 4.dp
    val sm = 8.dp
    val md = 12.dp
    val lg = 16.dp
    val xl = 24.dp
    val xxl = 32.dp
}

/** 分区标题：titleSmall SemiBold，可带数量与右侧操作槽 */
@Composable
fun WnSectionHeader(
    title: String,
    count: Int? = null,
    trailing: (@Composable RowScope.() -> Unit)? = null,
) {
    Row(
        Modifier.fillMaxWidth().padding(top = WnSpace.xs, bottom = WnSpace.xs),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(
            if (count != null) "$title（$count）" else title,
            style = MaterialTheme.typography.titleSmall,
            fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
        if (trailing != null) {
            Row(verticalAlignment = Alignment.CenterVertically) { trailing() }
        }
    }
}

/** 扁平卡片：1dp 发丝边框、无阴影、圆角 14dp、surface 底色；可选整卡点击（自带 ripple） */
@Composable
fun WnHairlineCard(
    modifier: Modifier = Modifier,
    onClick: (() -> Unit)? = null,
    content: @Composable ColumnScope.() -> Unit,
) {
    val m = modifier
        .fillMaxWidth()
        .defaultMinSize(minHeight = 48.dp)
    if (onClick != null) {
        Card(
            onClick = onClick,
            modifier = m,
            shape = WnCardShape,
            colors = CardDefaults.cardColors(containerColor = WnColors.surface),
            elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
            border = BorderStroke(1.dp, WnColors.line),
            content = content,
        )
    } else {
        Card(
            modifier = m,
            shape = WnCardShape,
            colors = CardDefaults.cardColors(containerColor = WnColors.surface),
            elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
            border = BorderStroke(1.dp, WnColors.line),
            content = content,
        )
    }
}

/** 空状态：大号矢量图标（inkDim）+ 标题 + 说明 + 纵向排列的操作按钮 */
@Composable
fun WnEmptyState(
    icon: ImageVector,
    title: String,
    body: String,
    modifier: Modifier = Modifier,
    actions: (@Composable ColumnScope.() -> Unit)? = null,
) {
    Column(
        modifier.fillMaxSize().padding(WnSpace.xl),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Icon(
            icon,
            contentDescription = null,
            tint = WnColors.inkDim,
            modifier = Modifier.size(56.dp),
        )
        Spacer(Modifier.height(WnSpace.lg))
        Text(title, style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.onSurface)
        Spacer(Modifier.height(WnSpace.sm))
        Text(
            body,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
        if (actions != null) {
            Spacer(Modifier.height(WnSpace.lg))
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(WnSpace.sm),
            ) { actions() }
        }
    }
}

/**
 * 排序下拉（书架「已缓存」子区块用）：两档「最近下载」「最近阅读」，
 * 当前项打勾；样式小巧（labelSmall + ArrowDropDown）。
 */
@Composable
fun WnSortMenu(current: String, onSelect: (String) -> Unit) {
    val options = listOf("downloaded" to "最近下载", "read" to "最近阅读")
    var expanded by remember { mutableStateOf(false) }
    val currentLabel = options.firstOrNull { it.first == current }?.second ?: options[0].second
    Box {
        TextButton(onClick = { expanded = true }) {
            Text(
                currentLabel,
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Icon(
                Icons.Filled.ArrowDropDown,
                contentDescription = null,
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.size(18.dp),
            )
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            options.forEach { (value, label) ->
                DropdownMenuItem(
                    text = {
                        Text(label, style = MaterialTheme.typography.labelMedium)
                    },
                    leadingIcon = {
                        if (value == current) {
                            Icon(
                                Icons.Filled.Check,
                                contentDescription = null,
                                tint = MaterialTheme.colorScheme.primary,
                                modifier = Modifier.size(18.dp),
                            )
                        } else {
                            Spacer(Modifier.size(18.dp))
                        }
                    },
                    onClick = {
                        expanded = false
                        if (value != current) onSelect(value)
                    },
                )
            }
        }
    }
}

/**
 * 分段页签（书架「最近阅读 / 已缓存」双页用）：pill 容器 + surfaceVar 底，
 * 选中段 accent 填充/onAccent 字色，未选中透明 + inkDim。
 */
@Composable
fun WnSegmentedTabs(
    options: List<String>,
    selected: Int,
    onSelect: (Int) -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier.fillMaxWidth().clip(WnPillShape).background(WnColors.surfaceVar)
            .padding(3.dp),
        horizontalArrangement = Arrangement.spacedBy(3.dp),
    ) {
        options.forEachIndexed { i, label ->
            val active = i == selected
            Box(
                Modifier.weight(1f).clip(WnPillShape)
                    .background(if (active) WnColors.accent else Color.Transparent)
                    .clickable { onSelect(i) }
                    .defaultMinSize(minHeight = 36.dp)
                    .padding(vertical = WnSpace.sm),
                Alignment.Center,
            ) {
                Text(
                    label,
                    style = MaterialTheme.typography.labelLarge,
                    fontWeight = if (active) FontWeight.SemiBold else FontWeight.Normal,
                    color = if (active) WnColors.onAccent else WnColors.inkDim,
                    maxLines = 1,
                )
            }
        }
    }
}

/** 无封面占位：灰色（surfaceVar）底 + 「暂无封面」注释，所有作品通用 */
@Composable
fun WnNoCover(modifier: Modifier = Modifier) {    Box(
        modifier.clip(WnChipShape).background(WnColors.surfaceVar),
        Alignment.Center,
    ) {
        Text(
            "暂无封面",
            style = MaterialTheme.typography.labelSmall,
            color = WnColors.inkDim,
            textAlign = TextAlign.Center,
        )
    }
}

/**
 * 封面图（搜索/浏览结果用）：走引擎代理地址，失败自动重试（递增退避，最多 3 次）。
 *
 * 背景：代理 `/api/manga/cover` 在回源并发闸（3 路）满时回 503「封面排队中」，
 * 普通 AsyncImage 拿到 503 就停在错误态不再尝试——一屏封面同时请求时总会有几张
 * 卡在占位。这里用 key(retry) 重新发起请求，配合服务端预热与代理磁盘缓存，
 * 重试时基本已是缓存命中。
 */
@Composable
fun WnCoverImage(
    url: String,
    contentDescription: String?,
    loader: ImageLoader,
    modifier: Modifier = Modifier,
    contentScale: ContentScale = ContentScale.Crop,
) {
    if (url.isBlank()) {
        WnNoCover(modifier)
        return
    }
    var retry by remember(url) { mutableIntStateOf(0) }
    val scope = rememberCoroutineScope()
    val ctx = LocalContext.current
    key(retry) {
        AsyncImage(
            model = ImageRequest.Builder(ctx).data(url).crossfade(true)
                .listener(onError = { _, _ ->
                    if (retry < 3) {
                        scope.launch {
                            delay(900L * (retry + 1))
                            retry++
                        }
                    }
                })
                .build(),
            imageLoader = loader,
            contentDescription = contentDescription,
            contentScale = contentScale,
            modifier = modifier.clip(WnChipShape).background(WnColors.surfaceVar),
        )
    }
}

/** 可点击小标签（漫画详情页作者/标签 → 点击跳转该源搜索；对齐 web 端标签交互） */
@Composable
fun WnTagChip(text: String, accent: Boolean = false, onClick: () -> Unit) {
    Text(
        text,
        style = MaterialTheme.typography.labelSmall,
        color = if (accent) WnColors.accent else WnColors.ink,
        maxLines = 1,
        overflow = TextOverflow.Ellipsis,
        modifier = Modifier
            .clip(WnPillShape)
            .background(WnColors.surfaceVar)
            .border(1.dp, if (accent) WnColors.accent else WnColors.line, WnPillShape)
            .clickable(onClick = onClick)
            .padding(horizontal = WnSpace.sm, vertical = WnSpace.xs),
    )
}
