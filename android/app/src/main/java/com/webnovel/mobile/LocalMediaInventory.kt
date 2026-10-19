package com.webnovel.mobile

data class LocalMediaEntry(val id: String, val name: String, val partial: Boolean)
data class LocalMediaInventory(val source: String, val comicId: String, val media: List<LocalMediaEntry>)
