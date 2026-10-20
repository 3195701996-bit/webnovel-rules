package com.webnovel.mobile

import org.json.JSONArray
import org.json.JSONObject

/** On-demand metadata only: never copy the raw API response, image URLs or network settings. */
fun mangaCatalogReport(detail: MangaDetail, update: MangaUpdateCheck? = null): String {
    val rows = detail.readingChapters
    val entries = JSONArray()
    rows.take(500).forEach { row ->
        entries.put(JSONObject().put("id", row.id.take(256))
            .put("name", row.name.take(200)).put("group", row.group.take(128))
            .put("downloaded", row.id in detail.downloaded)
            .put("partial", row.id in detail.partialDownloaded))
    }
    val duplicates = JSONArray()
    rows.groupBy { it.name.trim() }.filter { (name, group) -> name.isNotBlank() && group.size > 1 }
        .entries.take(40).forEach { (name, group) ->
            duplicates.put(JSONObject().put("name", name.take(200))
                .put("ids", JSONArray(group.take(20).map { it.id.take(256) })))
        }
    return JSONObject().put("schema", 1).put("scope", "visible_catalog_metadata_only")
        .put("source", detail.source).put("comic_id", detail.comicId)
        .put("title", detail.title).put("local_only", detail.localOnly)
        .put("chapter_count", rows.size).put("omitted_chapters", (rows.size - 500).coerceAtLeast(0))
        .put("chapters", entries).put("same_name_entries", duplicates)
        .put("update_checked", update != null).apply {
            update?.let { value ->
                put("update_ok", value.ok)
                put("latest", value.latest)
                put("missing_count", value.missingCount)
                put("missing_ids", JSONArray(value.missingIds.take(200)))
            }
        }.toString(2)
}
