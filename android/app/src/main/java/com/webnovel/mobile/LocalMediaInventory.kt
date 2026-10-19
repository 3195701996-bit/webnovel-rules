package com.webnovel.mobile

data class LocalMediaEntry(val id: String, val name: String, val partial: Boolean)
data class LocalMediaInventory(val source: String, val comicId: String, val media: List<LocalMediaEntry>)

internal fun filterLocalMedia(rows: List<LocalMediaEntry>, query: String): List<LocalMediaEntry> {
    val terms = java.text.Normalizer.normalize(query, java.text.Normalizer.Form.NFKC)
        .trim().split(Regex("\\s+")).filter { it.isNotBlank() }
    if (terms.isEmpty()) return rows
    val numeric = Regex("[0-9]+(?:\\.[0-9]+)?")
    return rows.filter { row ->
        val title = java.text.Normalizer.normalize(row.name, java.text.Normalizer.Form.NFKC)
        val id = java.text.Normalizer.normalize(row.id, java.text.Normalizer.Form.NFKC)
        terms.all { term ->
            when {
                term.startsWith("id:", ignoreCase = true) -> term.length > 3 && id.contains(term.drop(3), ignoreCase = true)
                numeric.matches(term) -> title.contains(term)
                else -> "$title $id".contains(term, ignoreCase = true)
            }
        }
    }
}

internal fun toggleLocalMediaSelection(selected: Set<String>, visible: List<LocalMediaEntry>): Set<String> {
    val ids = visible.map { it.id }.toSet()
    return if (ids.isNotEmpty() && selected.containsAll(ids)) selected - ids else selected + ids
}
