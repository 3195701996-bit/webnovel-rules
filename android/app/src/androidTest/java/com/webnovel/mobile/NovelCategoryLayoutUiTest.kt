package com.webnovel.mobile

import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.getUnclippedBoundsInRoot
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import android.graphics.Bitmap
import java.io.File

class NovelCategoryLayoutUiTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()
    @Before fun guard() {
        val qemu = ProcessBuilder("getprop", "ro.kernel.qemu").start().inputStream
            .bufferedReader().use { it.readText().trim() }
        assertEquals("分类布局合成测试仅限模拟器", "1", qemu)
    }
    private fun verify(scale: Float) {
        val entries = listOf(
            ExploreCategory("总点击", "排行榜", "/one"),
            ExploreCategory("日点击", "排行榜", "/two"),
            ExploreCategory("很长的分类标题应该自然换行并保留完整文字而不是挤成竖排", "题材", "/long"),
            ExploreCategory("总点击", "排行榜", "/distinct"),
        )
        var selected: ExploreCategory? = null
        val density = rule.activity.resources.displayMetrics.density
        rule.activityRule.scenario.onActivity { activity -> activity.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density, scale)) {
                MaterialTheme {
                    Column(Modifier.width(360.dp).statusBarsPadding().padding(16.dp)
                        .verticalScroll(rememberScrollState())) {
                        NovelCategoryMenu(entries, "fixture") { selected = it }
                    }
                }
            }
        } }
        rule.waitForIdle()
        assertEquals("重复分组不合并，顺序语义不变", 2,
            rule.onAllNodesWithText("排行榜").fetchSemanticsNodes().size)
        val first = rule.onNodeWithTag("novel_cat_fixture_/one").getUnclippedBoundsInRoot()
        val second = rule.onNodeWithTag("novel_cat_fixture_/two").getUnclippedBoundsInRoot()
        if (scale == 1f) assertEquals("短入口应同排而非全宽长按钮", first.top, second.top)
        val long = rule.onNodeWithTag("novel_cat_fixture_/long")
        long.performScrollTo()
        val bounds = long.getUnclippedBoundsInRoot()
        assertTrue("长标题应在视口内换行", bounds.right - bounds.left <= 360.dp)
        assertTrue("长标题不可挤成单字竖排", bounds.right - bounds.left > 180.dp)
        assertTrue("完整文字仍在语义树", rule.onAllNodesWithText(entries[2].title).fetchSemanticsNodes().isNotEmpty())
        rule.onNodeWithTag("novel_cat_fixture_/distinct").performScrollTo().performClick()
        rule.runOnIdle { assertSame("同名入口必须提交原始身份", entries[3], selected) }
        val screenshot = InstrumentationRegistry.getInstrumentation().uiAutomation.takeScreenshot()
        try {
            File(rule.activity.getExternalFilesDir(null), "novel-category-$scale.png")
                .outputStream().use { screenshot.compress(Bitmap.CompressFormat.PNG, 100, it) }
        } finally { screenshot.recycle() }
    }
    @Test fun compactGroupsKeepExactSelectionIdentity() = verify(1f)
    @Test fun enlargedTextWrapsWithoutOverflowOrLosingLabels() = verify(1.6f)
}
