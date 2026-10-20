package com.webnovel.mobile

import androidx.compose.ui.test.junit4.AndroidComposeTestRule
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.test.click
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.test.ext.junit.rules.ActivityScenarioRule

/** 测试夹具写入后回到根书架，并进入下载内容所在的「已缓存」分栏。 */
internal fun AndroidComposeTestRule<ActivityScenarioRule<MainActivity>, MainActivity>.openCachedShelf() {
    // Activity.recreate() is used by fixtures so the app reloads files written in @Before.
    // Wait for the root tab's visible label, then select its navigation item. The icon's
    // contentDescription is merged with the label and is not a reliable separate selector.
    waitUntil(60_000) {
        runCatching { onAllNodesWithText("书架").fetchSemanticsNodes().isNotEmpty() }
            .getOrDefault(false)
    }
    val shelfLabels = onAllNodesWithText("书架").fetchSemanticsNodes()
    onAllNodesWithText("书架")[shelfLabels.lastIndex].performClick()
    waitUntil(30_000) { onAllNodesWithText("已缓存").fetchSemanticsNodes().isNotEmpty() }
    onNodeWithText("已缓存").performClick()
}

/** 阅读会话初始为沉浸态；测试顶部章名/进度/返回前，按真实交互点中心唤出控件。 */
internal fun AndroidComposeTestRule<ActivityScenarioRule<MainActivity>, MainActivity>
    .tapReaderCenterAndShowControls() {
    val pageTag = if (onAllNodesWithTag("manga_pages").fetchSemanticsNodes().isNotEmpty()) {
        "manga_pages"
    } else {
        "manga_pager"
    }
    onNodeWithTag(pageTag).performTouchInput {
        click(Offset(width / 2f, height / 2f))
    }
    waitUntil(10_000) {
        onAllNodesWithTag("reader_progress_label").fetchSemanticsNodes().isNotEmpty()
    }
}
