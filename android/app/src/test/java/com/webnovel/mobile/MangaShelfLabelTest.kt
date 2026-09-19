package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Test

/** 漫画书架标签：**本机没内容时不许说"未读"**（实测书库里有这种记录：演示源残留）。 */
class MangaShelfLabelTest {
    private fun item(localImages: Int, status: String = "done",
                     readPos: String = "", dlDone: Int = 0, dlTotal: Int = 0): MangaItem {
        val o = org.json.JSONObject()
            .put("source", "s").put("comic_id", "c").put("title", "t")
            .put("source_name", "演示卷源").put("local_images", localImages)
            .put("status", status).put("read_pos", readPos).put("read_total", 0)
            .put("read_ratio", 0).put("read_idx", 0)
            .put("dl_done", dlDone).put("dl_total", dlTotal)
        return EngineData.manga("""{"comics":[${o}]}""").first()
    }

    @Test fun noLocalFilesIsNotUnread() {
        val it = item(localImages = 0)
        assertEquals(true, it.localEmpty)
        assertEquals("本机没有内容（长按可删除记录）", it.progressLabel)
    }

    @Test fun downloadedShowsImageCount() {
        assertEquals("已下载 · 42 图", item(localImages = 42).progressLabel)
    }

    @Test fun downloadingWins() {
        assertEquals("下载中 3/10 话",
            item(localImages = 0, status = "downloading", dlDone = 3, dlTotal = 10).progressLabel)
    }

    @Test fun readPositionWins() {
        assertEquals("读到 第7话", item(localImages = 42, readPos = "第7话").progressLabel)
    }

    @Test fun localEmptyNeedsNoDownloadInFlight() {
        assertEquals(false, item(localImages = 0, status = "downloading").localEmpty)
        assertEquals(false, item(localImages = 1).localEmpty)
    }
}
