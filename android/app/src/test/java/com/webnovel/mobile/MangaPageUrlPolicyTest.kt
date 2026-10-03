package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Test

class MangaPageUrlPolicyTest {
    @Test fun localCatalogOverridesConflictingModeAndPreservesOtherQuery() {
        assertEquals(
            "/api/manga/jm/42/chapter/ch/img/0?token=abc&catalog=local",
            enforceLocalCatalog(
                "/api/manga/jm/42/chapter/ch/img/0?token=abc&catalog=online"),
        )
    }

    @Test fun localCatalogAddsMissingModeAndRetryWithoutDroppingQuery() {
        val local = enforceLocalCatalog("/api/manga/jm/42/chapter/ch/img/0?token=abc")
        assertEquals(
            "http://127.0.0.1:8765$local&r=3",
            appendImageRetry("http://127.0.0.1:8765$local", 3),
        )
    }

    @Test fun retryDoesNotRewriteUrlWhenTickIsZero() {
        val url = "http://127.0.0.1:8765/api/manga/jm/42/chapter/ch/img/0?catalog=local"
        assertEquals(url, appendImageRetry(url, 0))
    }
}
