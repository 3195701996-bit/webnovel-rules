package com.webnovel.mobile

/** Filter the latest server snapshot; never infer read state from the resume position. */
internal fun filterMangaFavorites(
    favorites: List<MangaFavorite>, query: String, unreadOnly: Boolean,
): List<MangaFavorite> {
    val terms = query.trim().split(Regex("\\s+")).filter { it.isNotEmpty() }
    return favorites.filter { favorite ->
        (!unreadOnly || favorite.unreadCount > 0) && terms.all { term ->
            favorite.title.contains(term, ignoreCase = true) ||
                favorite.source.contains(term, ignoreCase = true)
        }
    }
}
