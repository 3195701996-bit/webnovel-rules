package com.webnovel.mobile

internal data class DownloadTaskSections(
    val running: List<EngineData.TaskItem>,
    val paused: List<EngineData.TaskItem>,
    val finished: List<EngineData.TaskItem>,
)

/** Preserve every server record and its order, with exactly one actionable section. */
internal fun downloadTaskSections(tasks: List<EngineData.TaskItem>): DownloadTaskSections {
    val running = mutableListOf<EngineData.TaskItem>()
    val paused = mutableListOf<EngineData.TaskItem>()
    val finished = mutableListOf<EngineData.TaskItem>()
    for (task in tasks) {
        when {
            task.running || task.status == "queued" -> running.add(task)
            task.status == "paused" || task.status == "stopped" || task.status == "error" -> paused.add(task)
            else -> finished.add(task)
        }
    }
    return DownloadTaskSections(running, paused, finished)
}
