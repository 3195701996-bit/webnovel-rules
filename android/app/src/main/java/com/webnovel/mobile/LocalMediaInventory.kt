package com.webnovel.mobile

data class LocalMediaEntry(val id: String, val name: String, val partial: Boolean)
data class LocalMediaInventory(val source: String, val comicId: String, val media: List<LocalMediaEntry>)

internal fun filterLocalMedia(rows: List<LocalMediaEntry>, query: String): List<LocalMediaEntry> {
    val terms = query.trim().split(Regex("\\s+")).filter { it.isNotBlank() }
    if (terms.isEmpty()) return rows
    return rows.filter { row ->
        val searchable = "${row.name} ${row.id}"
        terms.all { searchable.contains(it, ignoreCase = true) }
    }
}

internal fun toggleLocalMediaSelection(selected: Set<String>, visible: List<LocalMediaEntry>): Set<String> {
    val ids = visible.map { it.id }.toSet()
    return if (ids.isNotEmpty() && selected.containsAll(ids)) selected - ids else selected + ids
}
