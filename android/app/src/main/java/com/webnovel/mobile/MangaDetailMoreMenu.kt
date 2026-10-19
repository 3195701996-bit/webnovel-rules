package com.webnovel.mobile

import androidx.compose.foundation.layout.Box
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag

/** Low-frequency utilities stay available without displacing the chapter catalog. */
@Composable
internal fun MangaDetailMoreMenu(onRemove: () -> Unit, onCopyCatalog: () -> Unit,
    enabled: Boolean = true, onLocalMedia: (() -> Unit)? = null) {
    var expanded by remember { mutableStateOf(false) }
    Box {
        TextButton(onClick = { expanded = true }, enabled = enabled,
            modifier = Modifier.testTag("manga_detail_more")) {
            Text("更多")
        }
        DropdownMenu(expanded = expanded && enabled, onDismissRequest = { expanded = false }) {
            if (onLocalMedia != null) DropdownMenuItem(text = { Text("本地缓存管理") }, enabled = enabled,
                onClick = { expanded = false; onLocalMedia() }, modifier = Modifier.testTag("manage_local_media"))
            DropdownMenuItem(text = { Text("从书库移除") }, enabled = enabled, onClick = {
                expanded = false
                onRemove()
            })
            DropdownMenuItem(text = { Text("复制章节核对信息") }, enabled = enabled, onClick = {
                expanded = false
                onCopyCatalog()
            }, modifier = Modifier.testTag("copy_catalog_report"))
        }
    }
}
