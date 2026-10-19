package com.webnovel.mobile

import org.junit.Assert.*
import org.junit.Test

class LocalMediaSelectionTest {
    private val rows = listOf(LocalMediaEntry("old-ID", "第01卷", true),
        LocalMediaEntry("new-ID", "第01卷", false), LocalMediaEntry("episode-42", "第42话", false))

    @Test fun searchUsesAllTermsWithoutCollapsingSameNames() {
        assertEquals(rows.take(2), filterLocalMedia(rows, "第01卷"))
        assertEquals(rows.take(1), filterLocalMedia(rows, " 第01卷  OLD "))
        assertEquals(emptyList<LocalMediaEntry>(), filterLocalMedia(rows, "第01卷 episode"))
        assertSame(rows, filterLocalMedia(rows, "  "))
    }

    @Test fun scopedSelectionKeepsHiddenIdsAndEmptyScopeDoesNothing() {
        val hidden = setOf("episode-42")
        val selected = toggleLocalMediaSelection(hidden, rows.take(2))
        assertEquals(rows.map { it.id }.toSet(), selected)
        assertEquals(hidden, toggleLocalMediaSelection(selected, rows.take(2)))
        assertEquals(hidden, toggleLocalMediaSelection(hidden, emptyList()))
        assertEquals(setOf("old-ID"), toggleLocalMediaSelection(emptySet(), rows.take(1)))
    }

    @Test fun numericTitleQueryDoesNotMatchIncidentalUuidDigits() {
        val sample = listOf(LocalMediaEntry("uuid-52-old", "第13话", false),
            LocalMediaEntry("uuid-new", "第52话", false), LocalMediaEntry("uuid-2.2", "第3话", false),
            LocalMediaEntry("fractional", "第2.2话", false))
        assertEquals(listOf(sample[1]), filterLocalMedia(sample, "52"))
        assertEquals(listOf(sample[1]), filterLocalMedia(sample, "５２"))
        assertEquals(listOf(sample[0]), filterLocalMedia(sample, "ID：52"))
        assertEquals(listOf(sample[3]), filterLocalMedia(sample, "2.2"))
        assertEquals(emptyList<LocalMediaEntry>(), filterLocalMedia(sample, "id:"))
    }
}
