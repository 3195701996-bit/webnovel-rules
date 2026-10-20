package com.webnovel.mobile

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp

/** Preserve original volume-first indexes; filtered indexes are never reader indexes. */
fun findMangaChapters(rows: List<MangaChapter>, query: String): List<IndexedValue<MangaChapter>> {
    val key = MangaResumeResolver.labelKey(query)
    return rows.withIndex().filter { (_, row) ->
        key.isBlank() || MangaResumeResolver.labelKey(row.name + " " + row.group).contains(key)
    }.take(100).toList()
}

@Composable
fun MangaChapterFinder(rows: List<MangaChapter>, onDismiss: () -> Unit,
                       onSelect: (Int, MangaChapter) -> Unit) {
    var query by remember { mutableStateOf("") }
    val matches = remember(rows, query) { findMangaChapters(rows, query) }
    val identityHints = remember(rows) { mangaChapterIdentityHints(rows) }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("查找章节") },
        text = {
            Column {
                OutlinedTextField(value = query, onValueChange = { query = it },
                    singleLine = true, label = { Text("输入话名、话号或分组") },
                    modifier = Modifier.fillMaxWidth().testTag("chapter_finder_query"))
                Text("最多显示 100 项；选择后从该章节第一页阅读。",
                    style = MaterialTheme.typography.bodySmall)
                if (matches.isEmpty()) Text("没有匹配的章节")
                LazyColumn(Modifier.fillMaxWidth().heightIn(max = 320.dp)) {
                    items(matches, key = { it.index }) { match ->
                        TextButton(onClick = { onSelect(match.index, match.value) },
                            modifier = Modifier.fillMaxWidth()
                                .testTag("find_chapter_${match.value.id}")) {
                            Column(Modifier.fillMaxWidth()) {
                                Text("${match.index + 1}. ${match.value.label}")
                                if (match.value.group.isNotBlank()) Text(match.value.group,
                                    style = MaterialTheme.typography.bodySmall)
                                identityHints[match.value.id]?.let { hint ->
                                    Text("章节身份：$hint",
                                        style = MaterialTheme.typography.labelSmall)
                                }
                            }
                        }
                    }
                }
            }
        }, confirmButton = { TextButton(onClick = onDismiss) { Text("关闭") } })
}

/** UUIDs often share a node suffix. Use the shortest distinguishing prefix instead. */
fun mangaChapterIdentityHints(rows: List<MangaChapter>): Map<String, String> = buildMap {
    rows.filter { it.name.isNotBlank() && it.id.isNotBlank() }
        .groupBy { it.name.trim() }.values.forEach { group ->
            val ids = group.map { it.id }.distinct()
            if (ids.size < 2) return@forEach
            val longest = ids.maxOf { it.length }
            var length = 8
            while (length < longest && ids.map { it.take(length) }.toSet().size < ids.size) length++
            ids.forEach { id -> put(id, id.take(length) + if (id.length > length) "…" else "") }
        }
}
