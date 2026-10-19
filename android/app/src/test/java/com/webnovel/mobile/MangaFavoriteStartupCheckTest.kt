package com.webnovel.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class MangaFavoriteStartupCheckTest {
    @Test
    fun newAppLaunchChecksAgainEvenWhenTheEngineInstanceSurvives() {
        val engineInstance = "foreground-engine-survived-app-close"
        MangaFavoriteStartupCheck.beginAppLaunch()
        try {
            assertTrue(MangaFavoriteStartupCheck.claim(engineInstance))
            assertFalse("同一次 App 启动不能重复检查",
                MangaFavoriteStartupCheck.claim(engineInstance))

            MangaFavoriteStartupCheck.beginAppLaunch()
            assertTrue("新 App 启动即使复用同一引擎实例也必须重新检查",
                MangaFavoriteStartupCheck.claim(engineInstance))
        } finally {
            MangaFavoriteStartupCheck.release(engineInstance)
        }
    }

    @Test
    fun claimIsOncePerEngineInstanceAndNewInstanceCanCheckAgain() {
        val first = "engine-instance-reused-port-a"
        val restarted = "engine-instance-reused-port-b"
        MangaFavoriteStartupCheck.release(first)
        MangaFavoriteStartupCheck.release(restarted)
        try {
            assertTrue(MangaFavoriteStartupCheck.claim(first))
            assertFalse("同一 Python 引擎实例不得重复启动检查",
                MangaFavoriteStartupCheck.claim(first))
            assertTrue("即使复用端口，新引擎实例也必须启动新一轮检查",
                MangaFavoriteStartupCheck.claim(restarted))
        } finally {
            MangaFavoriteStartupCheck.release(first)
            MangaFavoriteStartupCheck.release(restarted)
        }
    }

    @Test
    fun releasingFailedAttemptAllowsRetryButBlankIdentityIsRejected() {
        val instance = "engine-instance-retry"
        MangaFavoriteStartupCheck.release(instance)
        try {
            assertFalse("未取得可靠引擎身份时不能占用检查标记",
                MangaFavoriteStartupCheck.claim(" "))
            assertTrue(MangaFavoriteStartupCheck.claim(instance))
            MangaFavoriteStartupCheck.release(instance)
            assertTrue("失败释放标记后允许用户重试", MangaFavoriteStartupCheck.claim(instance))
        } finally {
            MangaFavoriteStartupCheck.release(instance)
        }
    }

    @Test
    fun emptyFavoriteSetDoesNotAttachToStaleCheckButRunningBatchIsObserved() {
        val empty = EngineData.mangaFavoriteCheckStart(
            """{"ok":true,"started":0,"already_running":false}""")!!
        val running = EngineData.mangaFavoriteCheckStart(
            """{"ok":true,"started":0,"already_running":true}""")!!
        val started = EngineData.mangaFavoriteCheckStart(
            """{"ok":true,"started":4}""")!!

        assertFalse(MangaFavoriteStartupCheck.shouldWatch(empty))
        assertTrue(MangaFavoriteStartupCheck.shouldWatch(running))
        assertTrue(MangaFavoriteStartupCheck.shouldWatch(started))
        assertNull(EngineData.mangaFavoriteCheckStart("not json"))
    }

    @Test
    fun checkStatusShowsProgressAndPartialFailureAction() {
        assertEquals("正在检查收藏更新（2/5）…",
            MangaFavoriteStartupCheck.statusMessage(
                MangaFavoriteCheckStatus(true, 5, 2, 1, 0)))
        assertEquals("收藏更新检查完成：3 部成功，2 部失败，可重试",
            MangaFavoriteStartupCheck.statusMessage(
                MangaFavoriteCheckStatus(false, 5, 5, 3, 2)))
        assertNull(MangaFavoriteStartupCheck.statusMessage(
            MangaFavoriteCheckStatus(false, 5, 5, 5, 0)))
        assertNull(EngineData.mangaFavoriteCheckStatus("{}"))
        assertNull(EngineData.mangaFavoriteCheckStatus(
            """{"running":true,"total":2,"checked":8,"succeeded":-1,"failed":-3}"""))
    }

    @Test fun malformedCountsCannotBecomeAnEmptyOrCompletedCheck() {
        for (body in listOf("{}", """{"started":"0"}""", """{"started":-1}""",
            """{"started":1.5}""", """{"started":2147483648}""",
            """{"ok":false,"started":0}""", """{"started":0,"already_running":"true"}""")) {
            assertNull(body, EngineData.mangaFavoriteCheckStart(body))
        }
        for (body in listOf(
            """{"running":"false","total":0,"checked":0,"succeeded":0,"failed":0}""",
            """{"running":false,"total":2,"checked":1,"succeeded":1,"failed":0}""",
            """{"running":false,"total":2,"checked":2,"succeeded":1,"failed":0}""",
            """{"running":false,"total":"0","checked":0,"succeeded":0,"failed":0}""",
            """{"running":false,"total":0,"checked":0,"succeeded":2147483647,"failed":2147483647}""",
            """{"ok":false,"running":false,"total":0,"checked":0,"succeeded":0,"failed":0}""")) {
            assertNull(body, EngineData.mangaFavoriteCheckStatus(body))
        }
    }

    @Test fun runningSnapshotAllowsNonAtomicPerItemCounterPublication() {
        val state = EngineData.mangaFavoriteCheckStatus(
            """{"running":true,"total":2,"checked":0,"succeeded":1,"failed":0}""")!!
        assertTrue(state.running)
        assertEquals(0, state.checked)
        assertEquals(1, state.succeeded)
    }
}
