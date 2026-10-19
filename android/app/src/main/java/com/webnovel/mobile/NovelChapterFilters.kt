package com.webnovel.mobile

import java.text.Normalizer
import java.util.Locale

/** Display-only filtering: retain original order, objects and absolute directory indices. */
internal fun filterNovelChapters(chapters: List<ChapterItem>, query: String, mode: String): List<ChapterItem> {
    val normalized = Normalizer.normalize(query, Normalizer.Form.NFKC).trim().lowercase(Locale.ROOT)
    val numeric = normalized.takeIf { it.isNotEmpty() && it.all { c -> c in '0'..'9' } }
        ?.trimStart('0')?.ifEmpty { "0" }
    val terms = normalized.split(Regex("\\s+")).filter { it.isNotEmpty() }
    if (terms.isEmpty() && mode != "downloaded" && mode != "missing") return chapters
    return chapters.filter { chapter ->
        val stateMatches = when (mode) {
            "downloaded" -> chapter.downloaded
            "missing" -> !chapter.downloaded
            else -> true
        }
        stateMatches && when {
            numeric != null -> chapter.index.toString() == numeric
            terms.isEmpty() -> true
            else -> {
                val name = Normalizer.normalize(chapter.label, Normalizer.Form.NFKC).lowercase(Locale.ROOT)
                terms.all(name::contains)
            }
        }
    }
}
