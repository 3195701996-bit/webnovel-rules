package com.webnovel.mobile

internal data class DownloadTaskSections(
    val running: List<EngineData.TaskItem>,
    val paused: List<EngineData.TaskItem>,
    val finished: List<EngineData.TaskItem>,
)

internal fun EngineData.TaskItem.needsDownloadRefresh(): Boolean =
    running || status == "running" || status == "queued" || status == "cancel"

/** Display-only search: keep original task identity, order and queue state. */
internal fun filterDownloadTasks(tasks: List<EngineData.TaskItem>, query: String, mode: String): List<EngineData.TaskItem> {
    fun normalize(value: String) = java.text.Normalizer.normalize(value, java.text.Normalizer.Form.NFKC)
        .lowercase(java.util.Locale.ROOT)
    val words = normalize(query).trim().split(Regex("\\s+")).filter { it.isNotEmpty() }
    return tasks.filter { task ->
        val active = task.needsDownloadRefresh()
        val recoverable = !active && task.status in setOf("paused", "stopped", "error")
        val matchesMode = when (mode) {
            "active" -> active
            "recoverable" -> recoverable
            "failed" -> !active && task.status == "error"
            "finished" -> !active && !recoverable
            else -> true
        }
        val text = normalize(task.title + " " + task.mangaKey.substringBefore(':'))
        matchesMode && words.all { it in text }
    }
}

internal fun mangaResumeMessage(title: String, accepted: Boolean, resumed: Boolean, reason: String): String =
    when {
        accepted && resumed -> "已请求继续：$title"
        accepted && reason == "no_task" -> "任务记录已不存在，未启动下载；请从漫画详情重新选择章节：$title"
        else -> "未确认恢复下载，请刷新任务后重试：$title"
    }

/** Preserve every server record and its order, with exactly one actionable section. */
internal fun downloadTaskSections(tasks: List<EngineData.TaskItem>): DownloadTaskSections {
    val running = mutableListOf<EngineData.TaskItem>()
    val paused = mutableListOf<EngineData.TaskItem>()
    val finished = mutableListOf<EngineData.TaskItem>()
    for (task in tasks) {
        when {
            task.needsDownloadRefresh() -> running.add(task)
            task.status == "paused" || task.status == "stopped" || task.status == "error" -> paused.add(task)
            else -> finished.add(task)
        }
    }
    return DownloadTaskSections(running, paused, finished)
}
