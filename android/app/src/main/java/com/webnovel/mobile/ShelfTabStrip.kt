package com.webnovel.mobile

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

/** Consumer shelf navigation: text and a quiet underline, not a filled control panel. */
@Composable
internal fun ShelfTabStrip(
    options: List<String>, selected: Int, onSelect: (Int) -> Unit, modifier: Modifier = Modifier,
) {
    Row(modifier.fillMaxWidth().selectableGroup(), horizontalArrangement = Arrangement.Start) {
        options.forEachIndexed { index, label ->
            val active = index == selected
            Column(
                Modifier.weight(1f)
                    .selectable(selected = active, role = Role.Tab, onClick = { onSelect(index) })
                    .testTag("shelf_tab_$index")
                    .padding(vertical = 8.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text(label, style = MaterialTheme.typography.bodyLarge,
                    fontWeight = if (active) FontWeight.SemiBold else FontWeight.Medium,
                    color = if (active) WnColors.accent else WnColors.inkDim,
                    maxLines = 1, overflow = TextOverflow.Ellipsis)
                Spacer(Modifier.height(8.dp))
                Box(Modifier.width(40.dp).height(3.dp)
                    .background(if (active) WnColors.accent else Color.Transparent))
            }
        }
    }
}
