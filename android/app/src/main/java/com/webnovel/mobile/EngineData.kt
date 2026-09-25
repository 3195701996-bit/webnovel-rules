package com.webnovel.mobile

import android.net.Uri
import org.json.JSONObject

/** 小说条目（/api/books）：含**阅读进度**字段（读到第几章/章内百分比） */
data class NovelItem(
    val key: String,
    val name: String,
    val author: String,
    val total: Int,
    val done: Int,
    val percent: Int,
    val readIdx: Int,
    val readPct: Int,
    val readName: String,
    val readRatio: Int,
    val lastReadTs: Double,
    /** 最近下载/更新时间（服务端 `updated_at`，"yyyy-MM-dd HH:mm:ss"，字典序即时间序；旧记录可能为空串） */
    val updatedAt: String = "",
    /** 失败章节数（服务端 /api/books 的 `failed` 键；书架状态徽标用） */
    val failed: Int = 0,
) {
    val started: Boolean get() = readIdx > 0 || lastReadTs > 0
    val progressLabel: String
        get() = if (started) {
            val where = readName.ifBlank { "第 $readIdx 章" }
            "读到 $where" + if (readPct > 0) " · 本章 $readPct%" else ""
        } else if (percent >= 100) "已下载 · $done/$total 章" else "已下载 $done/$total 章"
}

/** 漫画条目（/api/manga/library）：含阅读进度与封面展示地址 */
data class MangaItem(
    val source: String,
    val comicId: String,
    val title: String,
    val sourceName: String,
    val localImages: Int,
    val status: String,
    val coverPath: String,
    val readIdx: Int,
    val readPos: String,
    val readTotal: Int,
    val readRatio: Int,
    val lastReadTs: Double,
    val dlDone: Int,
    val dlTotal: Int,
    /**
     * 续读定位是否**精确**（0.64.0）：服务端按章节身份（id → 章名 → 话号）在
     * 当前目录里定位上次读到的那一话；false 表示只能用近似（目录变了）。
     */
    val readExact: Boolean = true,
    /** 记录里的章名（用户上次读到的话）与被匹配到的章名 */
    val readLabel: String = "",
    val readMatchedLabel: String = "",
    /** 定位说明（目录变化时会写明"上次的『第47话』已不在目录里…"） */
    val readNote: String = "",
    /** 最近下载完成时间（服务端 `downloaded_at`，"yyyy-MM-dd HH:mm:ss"，字典序即时间序；旧记录可能为空串） */
    val downloadedAt: String = "",
) {
    val started: Boolean get() = readPos.isNotBlank() || lastReadTs > 0
    val downloading: Boolean get() = status == "downloading"
    /** 本机**一张图都没有**（书库有记录、磁盘没内容）—— 实测有这种条目（演示源残留、或文件被清掉） */
    val localEmpty: Boolean get() = localImages <= 0 && !downloading
    val progressLabel: String
        get() = when {
            downloading -> "下载中 ${dlDone}/${dlTotal.coerceAtLeast(1)} 话"
            readNote.isNotBlank() -> "读到 $readPos · ${readNote}"
            readPos.isNotBlank() -> "读到 $readPos" + if (readTotal > 0) " · $readRatio%" else ""
            localImages > 0 -> "已下载 · $localImages 图"
            // 记录还在、磁盘上却没有内容：**不能说"未读"**（那等于说"你还没读"，
            // 用户会去找章节却发现没有）→ 如实说明，并指出去哪儿删掉这条记录。
            localEmpty -> "本机没有内容（长按可删除记录）"
            else -> "未读"
        }
}

/**
 * 小说书源（GET /api/sources）：Legado 格式的源配置。
 * valid 为 null 表示**从未校验**（不是"通过"），界面据此显示"未校验"。
 */
data class BookSource(
    val uid: String,
    val name: String,
    val host: String,
    val enabled: Boolean,
    val valid: Boolean?,
    val validError: String,
    val validTestedAt: String,
    /**
     * 移动可用性（`/api/sources` 合并的台账字段，见 server/novel_catalog.py）。
     *
     * 与漫画侧同一口径：**未验证 ≠ 可用**；实测失败也要写出**卡在哪一步**。
     * 这里只承载服务端给的事实，客户端不自己推断"应该能用"。
     */
    val categoryLabel: String = "",
    val categoryReason: String = "",
    val failedStageLabel: String = "",
    val verifyStale: Boolean = false,
    val verifyCounts: org.json.JSONObject? = null,
) {
    /** 一句话说明这个源现在到底是什么状态（给卡片直接用） */
    val availabilityLabel: String
        get() = when {
            categoryLabel.isBlank() -> "可用性未记录"
            failedStageLabel.isNotBlank() -> "$categoryLabel · 卡在${failedStageLabel}阶段"
            else -> categoryLabel
        }

    /** 证据数字（搜索条数/目录章数/正文字数）；没有就返回空串（不编） */
    val evidenceLabel: String
        get() {
            val c = verifyCounts ?: return ""
            val parts = ArrayList<String>()
            if (c.has("search") && !c.isNull("search")) parts.add("搜索 ${c.optInt("search")} 条")
            if (c.has("toc") && !c.isNull("toc")) parts.add("目录 ${c.optInt("toc")} 章")
            if (c.has("chars") && !c.isNull("chars")) parts.add("正文 ${c.optInt("chars")} 字")
            return parts.joinToString(" · ")
        }
}

/** 小说搜索：同书多源（/api/search 的一个 group 里挂着多个源） */
data class NovelSearchSource(
    val sourceUid: String,
    val sourceName: String,
    val bookUrl: String,
    val lastChapter: String,
    val chapterCount: Int,
    val wordCount: String,
)

data class NovelSearchGroup(
    val name: String,
    val author: String,
    val intro: String,
    val cover: String,
    val sources: List<NovelSearchSource>,
)

/** 搜索总体情况：部分失败/超时源数要如实告诉用户，不能让人以为"就这么少" */
data class SearchMeta(
    val total: Int = 0,
    val partial: Boolean = false,
    val timedOutSources: Int = 0,
    val errors: Map<String, String> = emptyMap(),
    val hasMore: Boolean = false,
    val page: Int = 1,
    /**
     * 整轮搜索没有结果、且服务端判定**本机连不出去**（真断网）。
     * 真断网时把"没搜到"当成结论是错的：用户手机没信号时会以为源站全坏了。
     * 服务端只在有证据（逐源归因或本机实测）时才置位（engine/neterr.py）。
     */
    val networkDown: Boolean = false,
)

/**
 * 漫画搜索结果条目。
 *
 * 服务端 `results[]` 是**按 (source,id) 分组的组**：{title, author, cover, tags,
 * sources:[{id, source, source_name}]}——id/source 在 sources 里而不是顶层
 * （早先按顶层读 id 会得到空串，被用例当场抓到）。
 * 这里按 (source,id) 展平成条目：站内同名不同 id 是不同作品，不去重。
 */
data class MangaSearchHit(
    val source: String,
    val sourceName: String,
    val comicId: String,
    val title: String,
    val author: String,
    val cover: String,
    val tags: List<String>,
)

/**
 * 漫画更新检查结果（POST /api/manga/<source>/<id>/check-update）。
 * 服务端返回 ok/has_update/missing_count/latest/missing…，这里防御式读取：
 * 拿不到就按"未知/失败"如实呈现，不猜、也不把"检查失败"说成"已是最新"。
 */
data class MangaUpdateCheck(
    val ok: Boolean,
    val hasUpdate: Boolean,
    val missingCount: Int,
    val latest: String,
    val error: String,
    val missingIds: List<String>,
) {
    val label: String
        get() = when {
            !ok -> "检查失败：" + error.ifBlank { "原因未知" }
            hasUpdate || missingCount > 0 ->
                "发现 " + (if (missingCount > 0) missingCount else missingIds.size) + " 个新话" +
                    (if (latest.isNotBlank()) "（最新：$latest）" else "")
            else -> "已是最新" + (if (latest.isNotBlank()) "（最新：$latest）" else "")
        }
}

/**
 * 小说「检查更新」状态（GET /api/books/<key>/check-status）→ 界面文案。
 *
 * 为什么单独建模（与漫画侧 MangaUpdateCheck 同因）：这段文案是**用户唯一的判断依据**，
 * 写死在 Composable 里就没法被测到。史上有两个真实的坑：
 *   · 缺失数曾用 `new_chapters + retry_failed` 计算，漏掉"本地未下载"那一类 ——
 *     一本下到一半的书（如 50/1306 章）会显示"已是最新（无新增/失败章节）"，
 *     而服务端其实有 1256 章可补。服务端一直返回 missing_count，界面没用。
 *   · 目录没取全 / 源站不可达时，服务端在 message 里如实说明，界面若吞掉就变成
 *     "已是最新"，等于对用户撒谎。
 */
data class NovelUpdateCheck(
    val ok: Boolean,
    val status: String,
    val missingCount: Int,
    val newChapters: Int,
    val retryFailed: Int,
    val serverMessage: String,
    val error: String,
    /** 老服务端没有 missing_count 字段时为 false → 回退到 nw + rf */
    val hasMissingCount: Boolean,
) {
    val label: String
        get() = when {
            status == "error" || (!ok && error.isNotBlank()) ->
                "检查失败：" + error.ifBlank { "原因未知" }
            missingCount > 0 ->
                if (newChapters + retryFailed > 0)
                    "发现 $newChapters 个新章节、$retryFailed 个失败章节（共 $missingCount 章待补）"
                else "有 $missingCount 个章节尚未下载"
            // 服务端 message 里可能带"目录未取全（撞分页上限）"/"源站目录本次不可达"
            // —— 这类如实告知必须透出，不能吞掉后只说"已是最新"。
            serverMessage.contains("未取全") || serverMessage.contains("不可达") -> serverMessage
            else -> "已是最新（无新增/失败章节）"
        }
}

/** 书库批量检查更新的进度与逐部结果（GET /api/manga/library/check-updates/status） */
data class MangaLibraryUpdate(
    val running: Boolean,
    val done: Int,
    val total: Int,
    val items: List<Pair<String, MangaUpdateCheck>>,
) {
    val withUpdate: List<Pair<String, MangaUpdateCheck>>
        get() = items.filter { it.second.ok && (it.second.hasUpdate || it.second.missingCount > 0) }

    val label: String
        get() = if (running) "检查中 $done/$total"
               else when {
                   items.isEmpty() -> "尚未检查"
                   withUpdate.isEmpty() -> "全部已是最新（检查 ${items.size} 部）"
                   else -> "${withUpdate.size} 部有新话（检查 ${items.size} 部）"
               }
}

/** 探索分类入口（/api/explore/sources 的 categories[]） */
data class ExploreCategory(val title: String, val group: String, val url: String)

/** 提供榜单/分类的源：只有声明了 exploreUrl/ruleExplore 的源才会出现 */
data class ExploreSource(
    val uid: String,
    val name: String,
    val enabled: Boolean,
    val categories: List<ExploreCategory>,
)

/** 探索书单条目（/api/explore 的 books[]） */
data class ExploreBook(
    val name: String,
    val author: String,
    val bookUrl: String,
    val cover: String,
    val sourceUid: String,
    val sourceName: String,
    val lastChapter: String,
)

/**
 * 逐源**功能验证**结果（GET /api/sources/verify/results）。
 *
 * 状态含义（由服务端判定，界面照实展示，不把未验证当通过）：
 *   verified    搜索/目录/正文三阶段都有真实证据
 *   partial     搜索通过，目录或正文阶段失败
 *   failed      搜索阶段就失败
 *   unsupported 规则需要 JS，或该源用到的规则类型缺少依赖
 */
data class SourceVerification(
    val uid: String,
    val name: String,
    val status: String,
    val reason: String,
    val testedAt: String,
    val searchCount: Int,
    val tocCount: Int,
    val contentChars: Int,
    val note: String,
) {
    val label: String
        get() = when (status) {
            "verified" -> "通过（搜 $searchCount / 目录 $tocCount 章 / 正文 $contentChars 字）" +
                (if (note.isNotBlank()) " · $note" else "")
            "partial" -> "部分通过：$reason"
            "failed" -> "失败：$reason"
            "unsupported" -> "未支持：$reason"
            "skipped" -> "本轮未纳入（已停用）"
            else -> "未验证"
        }
    val ok: Boolean get() = status == "verified"
}

/**
 * 漫画源（/api/manga/sources）。
 *
 * 两个层面分开呈现，不互相冒充：
 *   status/reason  ← 能力台账的**依赖级**判定（supported/degraded/unsupported/pending）
 *   verifyStatus   ← **实测**结论（verified/partial/failed/unsupported/pending）
 */
data class MangaSource(
    val key: String,
    val name: String,
    val status: String,
    val reason: String,
    val verifyStatus: String,
    val verifyReason: String,
    val verifyTestedAt: String,
    /** 图片需要服务器做块还原（jm 等）；App 据此给出"重建图片缓存"入口 */
    val scrambled: Boolean = false,
    /** 本机还原算法版本；缓存里记的是旧版本时，用户可只重建这些章节 */
    val processVersion: Int = 0,
    /** 支持排序参数（如 jm 的 mr/mv/…）；不支持的源不给排序控件，避免"点了没用" */
    val supportsOrder: Boolean = false,
    /** 移动可用性分类（Android 已验证/降级/不支持/待验证/已弃用） */
    val categoryLabel: String = "",
    val categoryReason: String = "",
    /** 实测最早失败的阶段（search/detail/images）；空 = 没失败或未实测 */
    val failedStage: String = "",
) {
    val verifyLabel: String
        get() = when (verifyStatus) {
            "verified" -> "实测通过" + (if (verifyTestedAt.isNotBlank()) "（$verifyTestedAt）" else "")
            "partial" -> "实测部分通过：$verifyReason"
            "failed" -> "实测失败：$verifyReason"
            "unsupported" -> "实测未支持：$verifyReason"
            "skipped" -> "本轮跳过"
            else -> "未实测"
        }
}

/** 章节目录项（/api/books/<key> 的 chapters[]）：下载/失败状态照实呈现 */
data class ChapterItem(
    val index: Int,
    val name: String,
    val downloaded: Boolean,
    val failed: Boolean,
    val failedReason: String,
) {
    val label: String get() = name.ifBlank { "第 $index 章" }
}

/**
 * 书籍详情（/api/books/<key>）。
 *
 * 注意：详情里的 `progress` 是**书目录下的 _progress.json**，那是抓取任务进度
 * （status/completed/total），不是阅读位置；阅读位置来自全局 book_progress.json，
 * 由 GET /api/books/<key>/progress 读取，二者不能混用（自检用例专门锁住这点）。
 */
data class BookDetail(
    val key: String,
    val name: String,
    val author: String,
    val intro: String,
    val total: Int,
    val done: Int,
    val failedCount: Int,
    val crawlStatus: String,
    val crawlCompleted: Int,
    val crawlTotal: Int,
    val chapters: List<ChapterItem>,
) {
    /**
     * 继续阅读的落点：有阅读进度就回进度章，否则第一**已下载**章，再否则第 1 章。
     *
     * 0.74.21：**落点必须落在当前目录范围内**（对齐漫画侧 resumeIndex 的
     * `in chapters.indices` 纪律——那边 2026-09-17 就修过同类问题）。
     * 服务端 `GET /api/books/<key>/progress` 返回的是**原样存储**的位置、
     * 不做范围校验；而目录会变（源站删章、或换源后目录更短），旧进度就会指向
     * 不存在的章：阅读器显示「加载失败：HTTP 404 · 章节序号越界」，
     * 用户只是想继续读书，却看到一条像故障的报错。
     * 越界时收敛到目录里最远的可用章，而不是把错误抛给用户。
     */
    fun resumeIndex(p: ReadProgress?): Int {
        if (chapters.isEmpty()) return 1
        val wanted = if (p != null && p.idx >= 1) p.idx
                     else chapters.firstOrNull { it.downloaded }?.index ?: 1
        val lo = chapters.minOf { it.index }.coerceAtLeast(1)
        val hi = chapters.maxOf { it.index }.coerceAtLeast(lo)
        return wanted.coerceIn(lo, hi)
    }
}

/** 漫画目录项（/api/manga/<source>/<comic_id> 的 chapters[]） */
/**
 * 续读信息（服务端 `/api/manga/<source>/<comic_id>` 的 `resume` 字段，0.64.0）。
 *
 * `index` 是服务端在当前章节目录里**按身份**定位到的下标；`exact=false` 表示
 * 只能近似（目录变了：加更/删章/插番外），`note` 会说明发生了什么——界面必须
 * 把它显示出来，并且**不能**在近似落点上自动覆盖用户的阅读记录。
 */
data class MangaResume(
    val index: Int,
    val exact: Boolean,
    val by: String,
    val recordLabel: String,
    val matchedLabel: String,
    val note: String,
    val pos: String,
    /** 服务端命中/记录里的**章节 id**：客户端在自己的列表里再定位一次的硬身份 */
    val matchedId: String = "",
    val recordId: String = "",
    /** 服务端给出的页码（0 = 未提供） */
    val pageNum: Int = 0,
) {
    /** 上次读到的页码（服务端已解析；缺字段时退回从 "… P<页码>" 取） */
    val page: Int get() = if (pageNum > 0) pageNum else EngineData.pageFromPos(pos)
    val hasRecord: Boolean get() = index >= 0 || recordLabel.isNotBlank()
}

/**
 * 按**章节身份**在当前章节目录里定位下标（与服务端 `_resolve_reading_position` 同规则）。
 *
 * 为什么客户端也要有：详情页与阅读器是**各自**去取详情的（详情页拿到的列表可能是
 * 下载缓存里的、阅读器拿到的是源站最新列表），把下标从一屏传给另一屏会在两份列表
 * 不一致时指到别的一话。传身份、各自本地定位，才是稳的。
 *
 * 优先级：章节 id → 章名精确 → 章名归一化 → 话号唯一 → 服务端下标 → 兜底下标。
 */
object MangaResumeResolver {
    private val NOISE = listOf("第", "話", "话", "章", "回", "集", "卷", "節", "节", "篇")

    /** 全角→半角 + 去空白 + 统一"话/話" … */
    fun labelKey(text: String): String {
        val sb = StringBuilder()
        for (ch in text) {
            val c = when {
                ch.code in 0xFF10..0xFF19 -> ('0' + (ch.code - 0xFF10))   // ０-９
                ch.code == 0x3000 -> ' '
                ch.code == 0xFF0E -> '.'
                else -> ch
            }
            if (!c.isWhitespace()) sb.append(c)
        }
        var t = sb.toString().lowercase()
        for (n in NOISE) t = t.replace(n, "")
        return t.trim()
    }

    /** 从章名里取话号（"第23话"→23.0；"第023話"→23.0）；取不到返回 null */
    fun labelNumber(text: String): Double? {
        val half = StringBuilder()
        for (ch in text) {
            half.append(if (ch.code in 0xFF10..0xFF19) ('0' + (ch.code - 0xFF10)) else ch)
        }
        Regex("(\\d+(?:\\.\\d+)?)").find(half.toString())?.let {
            return it.groupValues[1].toDoubleOrNull()
        }
        // 中文数字（有界：一…九十九）
        val cjk = mapOf('零' to 0, '〇' to 0, '一' to 1, '二' to 2, '两' to 2, '三' to 3,
            '四' to 4, '五' to 5, '六' to 6, '七' to 7, '八' to 8, '九' to 9)
        val digits = half.filter { it in cjk || it == '十' }
        if (digits.isEmpty()) return null
        if ('十' in digits) {
            val (a, b) = digits.split('十', limit = 2).let { it[0] to it.getOrElse(1) { "" } }
            val tens = if (a.isEmpty()) 1 else (cjk[a.last()] ?: 1)
            val ones = if (b.isEmpty()) 0 else (cjk[b.last()] ?: 0)
            return (tens * 10 + ones).toDouble()
        }
        return (cjk[digits.last()] ?: 0).toDouble()
    }

    fun resolve(chapters: List<MangaChapter>, chapterId: String, label: String,
                fallbackIndex: Int): Int {
        if (chapters.isEmpty()) return -1
        if (chapterId.isNotBlank()) {
            val i = chapters.indexOfFirst { it.id == chapterId }
            if (i >= 0) return i
        }
        val want = label.trim()
        if (want.isNotBlank()) {
            chapters.indexOfFirst { it.label == want }.let { if (it >= 0) return it }
            val k = labelKey(want)
            if (k.isNotBlank()) {
                chapters.indexOfFirst { labelKey(it.label) == k }.let { if (it >= 0) return it }
            }
            labelNumber(want)?.let { num ->
                val same = chapters.indices.filter { labelNumber(chapters[it].label) == num }
                if (same.isNotEmpty()) return same.first()
            }
        }
        return if (fallbackIndex in chapters.indices) fallbackIndex else -1
    }
}

data class MangaChapter(val id: String, val name: String, val group: String) {
    val label: String get() = name.ifBlank { id }
}

/**
 * 漫画详情。`downloaded` 是服务端扫描出的**已下载章节 id 集合**
 * （判据是目录内确有图片文件），不是"点过就算"。
 */
data class MangaDetail(
    val source: String,
    val comicId: String,
    val title: String,
    val cover: String,
    val author: String,
    val intro: String,
    val tags: List<String>,
    val chapters: List<MangaChapter>,
    /** 整卷条目（copymanga 系详情返回 volumes；可下载单元，详情页排在话列表前面） */
    val volumes: List<MangaChapter> = emptyList(),
    val downloaded: Set<String>,
    val sourceName: String,
    /** 续读信息（服务端解析；没有阅读记录时为 null） */
    val resume: MangaResume? = null,
) {
    val downloadedCount: Int get() = (volumes + chapters).count { downloaded.contains(it.id) }
    /** 上次读到的章名（记录里的原话），用于向用户解释"要打开的是哪一话" */
    val resumeRecordLabel: String get() = resume?.recordLabel.orEmpty()

    /**
     * 继续阅读落点（0.64.0：**服务端按章节身份解析的结果优先**）。
     *
     * 为什么不再用下标：目录会变（加更/删章/插番外），记录里的 idx 指向的是
     * "当时那份目录"的第 N 项；目录一变就落到别的一话，表现为"书库显示读到
     * 第47话，点进去却是第1话"（用户 2026-09-17 反馈，开发机真实数据可复现：
     * 记录 idx=55 而当前目录只有 54 章）。服务端按 章节 id → 章名 → 话号 →
     * 最近话号 逐级定位，客户端只做兜底。
     */
    fun resumeIndex(readIdx: Int): Int {
        if (chapters.isEmpty()) return -1
        val r = resume
        if (r != null) {
            // 身份优先（章节 id / 章名 / 话号），服务端下标只作兜底：详情页与阅读器
            // 可能拿到**两份不同的列表**，下标不能跨屏传递。
            val byIdentity = MangaResumeResolver.resolve(
                chapters, r.matchedId.ifBlank { r.recordId }, r.recordLabel, -1)
            if (byIdentity >= 0) return byIdentity
            if (r.index in chapters.indices) return r.index
        }
        if (readIdx in chapters.indices) return readIdx
        return chapters.indexOfFirst { downloaded.contains(it.id) }.takeIf { it >= 0 } ?: 0
    }
}

/** 章节图片页（/api/manga/<source>/<comic_id>/chapter/<chapter_id>） */
data class MangaChapterPages(
    val count: Int,
    val local: Boolean,
    val images: List<String>,
    /**
     * 本章是**旧版图片处理缓存**（升级版 Venera 指南 §4 P0"旧缓存升级体验"）。
     *
     * 服务端在读取侧做处理版本判定；旧缓存必须让读者看到"需要联网重新获取"，
     * 而不是让他以为是自己手机解码坏了——所以这个标记要一路传到界面上。
     */
    val staleProcessing: Boolean = false,
    val staleReason: String = "",
    /** 服务端回显的章节 id：写进度前用它确认"正在显示的就是这一话" */
    val chapterId: String = "",
    /**
     * 每页加载方式（/urls 批量接口给出）：local=本地直出、lazy=服务器懒下载通道
     * （jm 等混淆源）、否则为源站 CDN 地址（客户端直连，不经引擎中转）。
     * 旧 /chapter 端点不含此信息时为空列表，调用方回退到 img/N 端点。
     */
    val entries: List<MangaPageEntry> = emptyList(),
) {
    /** 图片字节统一走本机 img 端点（本地已下载直接读盘，未下载才回源） */
    fun imageUrl(port: Int, source: String, comicId: String, chapterId: String, index: Int): String =
        "http://127.0.0.1:$port/api/manga/${Uri.encode(source)}/${Uri.encode(comicId)}" +
            "/chapter/${Uri.encode(chapterId)}/img/$index"
}

/** 章节单页加载方式（/chapter/<id>/urls 的 images[] 条目） */
data class MangaPageEntry(
    val url: String,
    val local: Boolean,
    val lazy: Boolean,
)

/** 阅读位置（GET/POST /api/books/<key>/progress，全局 book_progress.json） */
data class ReadProgress(val idx: Int, val pct: Int, val name: String) {
    val started: Boolean get() = idx >= 1
}

/** 章节正文（/api/books/<key>/chapter/<idx>） */
data class ChapterContent(
    val index: Int,
    val name: String,
    val downloaded: Boolean,
    val content: String,
    val prev: Int?,
    val next: Int?,
    val total: Int,
    /**
     * 未下载时的**服务端原因**（0.55.0：`reason` 字段）。
     *
     * 两种情形必须区分，否则用户看到的是同一句模糊的"没有缓存"：
     *   · "本章尚未下载" —— 任务还没抓到这一章；
     *   · "本章缓存文件已丢失（缓存被清理或书目录被任务重建）…" —— state 说下载过、
     *     磁盘上却没有，此时"在线获取本章"/"继续下载"能把它补回来。
     */
    val reason: String = "",
) {
    /**
     * 正文段落：后端按 "\n" 存纯文本。
     *
     * 章节缓存文件的格式是 "<章节名>\n\n<正文>"，所以 content 的第一行往往就是章节名；
     * 阅读器已经把章节名作为标题单独渲染，这里去掉重复的首行（仅当与章节名完全一致时），
     * 避免正文开头再出现一次标题。
     */
    val paragraphs: List<String>
        get() {
            val all = content.split('\n').map { it.trim() }.filter { it.isNotEmpty() }
            val head = name.trim()
            return if (head.isNotEmpty() && all.isNotEmpty() && all.first() == head) {
                all.drop(1)
            } else all
        }
}

/**
 * 备份范围（GET /api/backup/scope）：包含什么、明确不含什么，各自带**磁盘实扫**的体积。
 *
 * 为什么要有这个：用户看到"备份完成 2 MB"时会以为"整库都备份了"，
 * 而本机正文与图片可能有十几 GB。把两边的真实数字并排给出来，才谈得上"范围明确"。
 */
data class BackupScope(
    val includes: List<String>,
    val excludes: List<Triple<String, Long, String>>,   // 名称 / 字节 / 原因
    val srcFiles: Int,
    val srcBytes: Long,
    val progressFiles: List<Pair<String, Long>>,
    val includeBytes: Long,
    val excludedBytes: Long,
    val note: String,
)

/** 存储占用分类（GET /api/storage） */
data class StorageCategory(
    val key: String,
    val label: String,
    val kind: String,          // user_data / cache / trash
    val regenerable: Boolean,
    val note: String,
    val bytes: Long,
    val count: Int,
)

/** 逐书占用（小说）：已缓存章节序号一并带出，供"按章节清理"用 */
data class StorageBook(
    val key: String,
    val name: String,
    val author: String,
    val total: Int,
    val cached: Int,
    val cachedIndexes: List<Int>,
    val failed: Int,
    val bytes: Long,
    val stopReason: String,
)

/** 逐作品占用（漫画已下载） */
data class StorageManga(
    val source: String,
    val comicId: String,
    val title: String,
    val images: Int,
    val bytes: Long,
)

/** 逐源临时缓存 */
data class StorageMangaCache(val source: String, val works: Int, val bytes: Long)

/** 回收站条目 */
data class StorageTrashItem(val name: String, val bytes: Long)

/**
 * 存储占用总览（GET /api/storage）。
 *
 * 数字全部来自服务端磁盘实扫——客户端**不估算**，也**不自己换算**释放量，
 * 否则"清理了但空间没少"这种事就会被界面掩盖。
 */
data class StorageUsage(
    val totalBytes: Long,
    val categories: List<StorageCategory>,
    val novelBooks: List<StorageBook>,
    val mangaDownloads: List<StorageManga>,
    val mangaCache: List<StorageMangaCache>,
    val trashItems: List<StorageTrashItem>,
    val generatedAt: String,
)

object EngineData {

    /** GET /api/books/<key>/check-status → NovelUpdateCheck（解析失败按失败态，不猜成"已是最新"） */
    fun novelUpdateCheck(body: String): NovelUpdateCheck {
        val o = runCatching { JSONObject(body) }.getOrNull()
            ?: return NovelUpdateCheck(ok = false, status = "error", missingCount = 0,
                newChapters = 0, retryFailed = 0, serverMessage = "",
                error = "响应无法解析", hasMissingCount = false)
        val nw = o.optInt("new_chapters")
        val rf = o.optInt("retry_failed")
        val hasMc = o.has("missing_count")
        return NovelUpdateCheck(
            ok = o.optBoolean("ok", true),
            status = o.optString("status"),
            missingCount = if (hasMc) o.optInt("missing_count") else nw + rf,
            newChapters = nw,
            retryFailed = rf,
            serverMessage = o.optString("message"),
            error = o.optString("error"),
            hasMissingCount = hasMc,
        )
    }

    fun novels(body: String): List<NovelItem> {
        val arr = runCatching { JSONObject(body).optJSONArray("books") }.getOrNull() ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            val o = arr.optJSONObject(i) ?: return@mapNotNull null
            NovelItem(
                key = o.optString("key"),
                name = o.optString("name"),
                author = o.optString("author"),
                total = o.optInt("total"),
                done = o.optInt("done"),
                percent = o.optInt("percent"),
                readIdx = o.optInt("read_idx"),
                readPct = o.optInt("read_pct"),
                readName = o.optString("read_name"),
                readRatio = o.optInt("read_ratio"),
                lastReadTs = o.optDouble("last_read_ts", 0.0),
                updatedAt = o.optString("updated_at"),
                failed = o.optInt("failed"),
            )
        }
    }

    fun manga(body: String): List<MangaItem> {
        val arr = runCatching { JSONObject(body).optJSONArray("comics") }.getOrNull() ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            val o = arr.optJSONObject(i) ?: return@mapNotNull null
            MangaItem(
                source = o.optString("source"),
                comicId = o.optString("comic_id"),
                title = o.optString("title"),
                sourceName = o.optString("source_name"),
                localImages = o.optInt("local_images"),
                status = o.optString("status"),
                coverPath = o.optString("cover_view"),
                readIdx = o.optInt("read_idx"),
                readPos = o.optString("read_pos"),
                readTotal = o.optInt("read_total"),
                readRatio = o.optInt("read_ratio"),
                lastReadTs = o.optDouble("last_read_ts", 0.0),
                dlDone = o.optInt("dl_done"),
                dlTotal = o.optInt("dl_total"),
                readExact = o.optBoolean("read_exact", true),
                readLabel = o.optString("read_label"),
                readMatchedLabel = o.optString("read_matched_label"),
                readNote = o.optString("read_note"),
                downloadedAt = o.optString("downloaded_at"),
            )
        }
    }

    /** 小说书源列表（GET /api/sources） */
    fun bookSources(body: String): List<BookSource> {
        val arr = runCatching { JSONObject(body).optJSONArray("sources") }.getOrNull() ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            val o = arr.optJSONObject(i) ?: return@mapNotNull null
            BookSource(
                uid = o.optString("uid"),
                name = o.optString("bookSourceName").ifBlank { o.optString("uid") },
                host = o.optString("bookSourceUrl"),
                // 缺省视为启用（与 engine.source_mgr.import_sources 的默认一致）
                enabled = if (o.has("enabled")) o.optBoolean("enabled") else true,
                // 缺失/非布尔 → 视为未校验，不能默认成"通过"
                valid = if (o.has("valid") && !o.isNull("valid")) o.optBoolean("valid") else null,
                validError = o.optString("valid_error"),
                validTestedAt = o.optString("valid_tested_at"),
                categoryLabel = o.optString("category_label"),
                categoryReason = o.optString("category_reason"),
                failedStageLabel = o.optString("failed_stage_label"),
                verifyStale = o.optBoolean("verify_stale", false),
                verifyCounts = o.optJSONObject("verify_counts"),
            )
        }
    }

    fun sources(body: String): List<MangaSource> {
        val arr = runCatching { JSONObject(body).optJSONArray("sources") }.getOrNull() ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            val o = arr.optJSONObject(i) ?: return@mapNotNull null
            val v = o.optJSONObject("verify") ?: JSONObject()
            MangaSource(
                key = o.optString("key"),
                name = o.optString("name"),
                status = o.optString("status", "unknown"),
                reason = o.optString("reason"),
                verifyStatus = v.optString("status", "pending"),
                verifyReason = v.optString("reason"),
                verifyTestedAt = v.optString("tested_at"),
                // 混淆源（jm 等）：图片由服务器做块还原；处理版本用于"重建缓存"入口
                scrambled = o.optBoolean("scrambled"),
                processVersion = o.optInt("process_version"),
                // 该源是否支持排序参数（App 据此决定显示不显示排序选择，不瞎给控件）
                supportsOrder = o.optBoolean("supports_order"),
                // 移动可用性分类（路线 §4.4 五分类）+ 实测失败阶段（最早失败的那一步）
                categoryLabel = o.optString("category_label"),
                categoryReason = o.optString("category_reason"),
                failedStage = o.optJSONObject("verification")?.optString("failed_stage") ?: "",
            )
        }
    }

    /** 书籍详情（/api/books/<key>）；解析失败返回 null，由界面显示可读错误而不是空页 */
    fun bookDetail(body: String): BookDetail? {
        val o = runCatching { JSONObject(body) }.getOrNull() ?: return null
        if (!o.has("chapters")) return null
        val arr = o.optJSONArray("chapters")
        val chapters = (0 until (arr?.length() ?: 0)).mapNotNull { i ->
            val c = arr!!.optJSONObject(i) ?: return@mapNotNull null
            ChapterItem(
                index = c.optInt("index"),
                name = c.optString("name"),
                downloaded = c.optBoolean("downloaded"),
                failed = c.optBoolean("failed"),
                failedReason = c.optString("failed_reason"),
            )
        }
        val p = o.optJSONObject("progress") ?: JSONObject()
        return BookDetail(
            key = o.optString("key"),
            name = o.optString("name"),
            author = o.optString("author"),
            intro = o.optString("intro"),
            total = o.optInt("total"),
            done = o.optInt("done"),
            failedCount = o.optInt("failed_count"),
            crawlStatus = p.optString("status"),
            crawlCompleted = p.optInt("completed"),
            crawlTotal = p.optInt("total"),
            chapters = chapters,
        )
    }

    /** 阅读位置（GET /api/books/<key>/progress）：idx 缺省或 0 表示未读 */
    fun readProgress(body: String): ReadProgress? {
        val o = runCatching { JSONObject(body) }.getOrNull() ?: return null
        if (o.optBoolean("ok") == false) return null
        return ReadProgress(o.optInt("idx"), o.optInt("pct"), o.optString("name"))
    }

    /** 章节正文；下载未完成时 downloaded=false 且 content 为空（照实呈现，不假装有内容） */
    fun chapter(body: String): ChapterContent? {
        val o = runCatching { JSONObject(body) }.getOrNull() ?: return null
        if (!o.has("index")) return null
        return ChapterContent(
            index = o.optInt("index"),
            name = o.optString("name"),
            downloaded = o.optBoolean("downloaded"),
            content = o.optString("content"),
            prev = if (o.isNull("prev")) null else o.optInt("prev"),
            next = if (o.isNull("next")) null else o.optInt("next"),
            total = o.optInt("total"),
            reason = o.optString("reason"),
        )
    }

    /**
     * 单章重爬响应（POST /api/books/<key>/chapter/<idx>）：{ok, index, name, content, length}。
     * 只有 ok=true 且正文非空才算成功——失败时不返回内容，避免界面显示"空正文当作成功"。
     */
    fun recoveredContent(body: String): String? {
        val o = runCatching { JSONObject(body) }.getOrNull() ?: return null
        if (!o.optBoolean("ok")) return null
        val c = o.optString("content")
        return c.ifBlank { null }
    }

    /** 漫画详情（/api/manga/<source>/<comic_id>）：chapters + 已下载章节集合 */
    fun mangaDetail(body: String): MangaDetail? {
        val o = runCatching { JSONObject(body) }.getOrNull() ?: return null
        if (!o.has("chapters")) return null
        val arr = o.optJSONArray("chapters")
        val chapters = (0 until (arr?.length() ?: 0)).mapNotNull { i ->
            val c = arr!!.optJSONObject(i) ?: return@mapNotNull null
            MangaChapter(
                id = c.optString("id"),
                name = c.optString("name"),
                group = c.optString("group"),
            )
        }
        val dl = o.optJSONArray("downloaded")
        val downloaded = (0 until (dl?.length() ?: 0)).mapNotNull { dl?.optString(it) }.toSet()
        val tagsArr = o.optJSONArray("tags")
        val tags = (0 until (tagsArr?.length() ?: 0)).mapNotNull { tagsArr?.optString(it) }
        // 整卷条目（copymanga 系）：与话同构的 id/name/group
        val volArr = o.optJSONArray("volumes")
        val volumes = (0 until (volArr?.length() ?: 0)).mapNotNull { i ->
            volArr?.optJSONObject(i)?.let { c ->
                MangaChapter(
                    id = c.optString("id"),
                    name = c.optString("name"),
                    group = c.optString("group"),
                )
            }
        }
        return MangaDetail(
            source = o.optString("source"),
            // 0.64.0：**身份一律取 `comic_id`**（服务端四条返回路径都已兜底给出），
            // 只有旧包缺字段时才退回 "id"。此前固定读 "id"：源的 `d.id` 与请求用的
            // comic_id 并不总是同一个值（如 copymanga 的 id 是 UUID、请求用路径 slug），
            // 于是"保存进度用的键"与"书库/历史查找用的键"不是同一个 → 续读找不到记录。
            comicId = o.optString("comic_id").ifBlank { o.optString("id") },
            title = o.optString("title"),
            cover = o.optString("cover"),
            author = o.optString("author"),
            intro = o.optString("intro").ifBlank { o.optString("desc") },
            tags = tags,
            chapters = chapters,
            volumes = volumes,
            downloaded = downloaded,
            sourceName = o.optString("source_name"),
            resume = o.optJSONObject("resume")?.let { r ->
                MangaResume(
                    index = r.optInt("index", -1),
                    exact = r.optBoolean("exact", false),
                    by = r.optString("by"),
                    recordLabel = r.optString("record_label"),
                    matchedLabel = r.optString("matched_label"),
                    note = r.optString("note"),
                    pos = r.optString("pos"),
                    matchedId = r.optString("matched_id"),
                    recordId = r.optString("record_id"),
                    pageNum = r.optInt("page", 0),
                )
            },
        )
    }

    /** 章节图片页：count 是可用图片数，local=true 表示全部来自本地已下载文件 */
    fun mangaChapterPages(body: String): MangaChapterPages? {
        val o = runCatching { JSONObject(body) }.getOrNull() ?: return null
        if (!o.has("count")) return null
        val arr = o.optJSONArray("images")
        val images = (0 until (arr?.length() ?: 0)).mapNotNull { arr?.optString(it) }
        return MangaChapterPages(
            count = o.optInt("count"),
            local = o.optBoolean("local"),
            images = images,
            staleProcessing = o.optBoolean("stale_processing", false),
            staleReason = o.optString("stale_reason"),
            chapterId = o.optString("chapter_id"),
        )
    }

    /**
     * 章节图片批量接口（/chapter/<id>/urls）：一次返回每页的**加载方式**——
     * 本地直出 / 服务器懒下载通道 / 源站 CDN 直连（对齐网页端阅读器）。
     */
    fun mangaChapterUrls(body: String): MangaChapterPages? {
        val o = runCatching { JSONObject(body) }.getOrNull() ?: return null
        if (!o.has("count")) return null
        val arr = o.optJSONArray("images") ?: return null
        val entries = (0 until arr.length()).mapNotNull { i ->
            val e = arr.optJSONObject(i) ?: return@mapNotNull null
            MangaPageEntry(
                url = e.optString("url"),
                local = e.optBoolean("local"),
                lazy = e.optBoolean("lazy"),
            )
        }
        if (entries.isEmpty()) return null
        return MangaChapterPages(
            count = o.optInt("count"),
            local = o.optBoolean("local_only"),
            images = entries.map { it.url },
            staleProcessing = o.optBoolean("stale_processing", false),
            staleReason = o.optString("stale_reason"),
            chapterId = o.optString("chapter_id"),
            entries = entries,
        )
    }

    /** 漫画历史条目（GET /api/manga/history → history[]） */
    data class MangaHistory(val source: String, val comicId: String, val idx: Int,
                            val pos: String, val title: String, val ts: Double)

    /** 漫画阅读历史：与网页端共用 _history.json，因此两端可以互相续读 */
    fun mangaHistory(body: String): List<MangaHistory> {
        val arr = runCatching { JSONObject(body).optJSONArray("history") }.getOrNull()
            ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            val o = arr.optJSONObject(i) ?: return@mapNotNull null
            MangaHistory(
                source = o.optString("source"),
                comicId = o.optString("comic_id"),
                idx = o.optInt("idx", -1),
                pos = o.optString("pos"),
                title = o.optString("title"),
                ts = o.optDouble("ts", 0.0),
            )
        }
    }

    /** 从 "<章名> P<页码>" 里取页码；取不到按第 1 页（与网页端正则一致） */
    fun pageFromPos(pos: String): Int =
        Regex("P(\\d+)").find(pos)?.groupValues?.get(1)?.toIntOrNull()?.coerceAtLeast(1) ?: 1

    /**
     * 漫画下载任务状态（GET /api/manga/download/status?source&cid）：
     * 详情页「下载中/排队中」标注、实时进度与暂停/继续的数据源。
     */
    data class MangaDlStatus(
        val status: String,          // idle/queued/running/paused/stopped/error/done
        val current: String,
        val done: Int,
        val total: Int,
        val imagesDone: Int,
        val imagesTotal: Int,
        val speed: Double,
        val eta: Double,
        val stopReason: String,
        val chapterIds: Set<String>,
        val failedIds: Set<String>,
    ) {
        /** 进行中（排队或下载中） */
        val active: Boolean get() = status == "queued" || status == "running"
        /** 可继续（暂停/中断/失败） */
        val resumable: Boolean get() =
            status == "paused" || status == "stopped" || status == "error"
        /** 有任务记录（详情页据此显示下载管理区；idle/done 不显示） */
        val present: Boolean get() = status.isNotBlank() && status != "idle" && status != "done"

        /** 进度文案：章级 + 图级 + 失败数（数据来自服务端，不自己估算） */
        val progressLabel: String
            get() {
                val ch = if (total > 0) "$done/$total 话" else "准备中"
                val img = if (imagesTotal > 0) " · $imagesDone/$imagesTotal 图" else ""
                val fail = if (failedIds.isNotEmpty()) " · 失败 ${failedIds.size} 话" else ""
                val spd = if (status == "running" && speed > 0)
                    " · ${String.format(java.util.Locale.US, "%.1f", speed)} 图/秒" +
                        if (eta > 0) " · 约 ${eta.toInt()} 秒" else ""
                else ""
                return ch + img + fail + spd
            }
    }

    fun mangaDlStatus(body: String): MangaDlStatus {
        val o = runCatching { JSONObject(body) }.getOrNull() ?: return MangaDlStatus(
            "idle", "", 0, 0, 0, 0, 0.0, 0.0, "", emptySet(), emptySet())
        val ids = (o.optJSONArray("chapters")?.let { arr ->
            (0 until arr.length()).mapNotNull { i ->
                arr.optJSONObject(i)?.optString("id")?.takeIf { it.isNotBlank() }
                    ?: arr.optString(i).takeIf { it.isNotBlank() }
            }
        } ?: emptyList()).toSet()
        val failed = (o.optJSONArray("failed_ids")?.let { arr ->
            (0 until arr.length()).map { arr.optString(it) }.filter { it.isNotBlank() }
        } ?: emptyList()).toSet()
        return MangaDlStatus(
            status = o.optString("status", "idle"),
            current = o.optString("current"),
            done = o.optInt("done"),
            total = o.optInt("total"),
            imagesDone = o.optInt("images_done"),
            imagesTotal = o.optInt("images_total"),
            speed = o.optDouble("speed", 0.0),
            eta = o.optDouble("eta", 0.0),
            stopReason = o.optString("stop_reason"),
            chapterIds = ids,
            failedIds = failed,
        )
    }

    /** 阅读位置：漫画用 <章名> P<页码> 的约定（与网页端一致，两端可互相续读） */
    fun mangaPos(chapterName: String, page: Int): String = "${chapterName.ifBlank { "本章" }} P${page.coerceAtLeast(1)}"

    /**
     * 任务条目（GET /api/tasks）：小说爬取任务与漫画下载任务在同一列表里，
     * 因此下载页可以只用一个接口呈现全部队列。
     */
    data class TaskItem(
        val id: String,
        val type: String,          // "manga" 或小说任务类型
        val title: String,
        val status: String,
        val running: Boolean,
        val done: Int,
        val total: Int,
        val current: String,
        val speed: Double,
        val eta: Double,
        val error: String,
        val mangaKey: String,      // "source:comic_id"（漫画任务才有）
        /**
         * 任务**为什么停了**（P1-1，`/api/tasks` 的结构化字段）。
         *
         * kind 是稳定枚举（user_pause / user_stop / service_stopped /
         * process_restart / error / done），reason 是给用户看的话。
         * 二者都由服务端给出——客户端不自己编原因，否则就会替服务端撒谎。
         */
        val stopKind: String = "",
        val stopReason: String = "",
        val resumableFlag: Boolean = false,
        val checkpointDone: Int = 0,
        val checkpointTotal: Int = 0,
        val checkpointFailed: Int = 0,
        /** 图片级进度（漫画任务）：服务端 images_done/images_total */
        val imagesDone: Int = 0,
        val imagesTotal: Int = 0,
    ) {
        val isManga: Boolean get() = type == "manga"
        val pausable: Boolean get() = running || status == "paused"
        val resumable: Boolean get() = resumableFlag ||
            status == "paused" || status == "stopped" || status == "error" || status == "cancel"

        /**
         * 需要向用户解释"为什么停了"吗？
         * 只在**没在跑**且服务端给了原因时显示；完成/正常结束不啰嗦。
         */
        val showStopReason: Boolean
            get() = !running && stopReason.isNotBlank() && status != "done"

        /** 断点提示："已下载 7/100 章（2 章失败），继续会从这里接上" */
        val checkpointLabel: String
            get() {
                if (checkpointTotal <= 0) return ""
                val failed = if (checkpointFailed > 0) "，${checkpointFailed} 章失败" else ""
                return "已下载 $checkpointDone/$checkpointTotal$failed"
            }
        val percent: Int
            get() = when {
                total > 0 -> (done * 100 / total).coerceIn(0, 100)
                status == "done" -> 100
                else -> 0
            }
        val label: String
            get() = when (status) {
                "running" -> "进行中 $done/$total"
                "queued" -> "排队中"
                "paused" -> "已暂停 $done/$total"
                "stopped" -> "已停止 $done/$total"
                "done" -> "已完成 $total"
                "cancel" -> "已取消 $done/$total"
                "error" -> "失败：${error.ifBlank { "原因未知" }}"
                else -> status.ifBlank { "未知状态" }
            }
        val speedLabel: String
            get() = if (running && speed > 0) {
                " · ${String.format(java.util.Locale.US, "%.1f", speed)} 项/秒" +
                    if (eta > 0) " · 约 ${eta.toInt()} 秒" else ""
            } else ""
        /** 漫画任务用 "source:comic_id" 拆出源与 id，供暂停/继续接口使用 */
        val mangaSource: String get() = mangaKey.substringBefore(":", "")
        val mangaComicId: String get() = mangaKey.substringAfter(":", "")
    }

    fun tasks(body: String): List<TaskItem> {
        val arr = runCatching { JSONObject(body).optJSONArray("tasks") }.getOrNull() ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            val o = arr.optJSONObject(i) ?: return@mapNotNull null
            val p = o.optJSONObject("progress") ?: JSONObject()
            val isMangaTask = o.optString("type") == "manga"
            TaskItem(
                id = o.optString("id"),
                type = o.optString("type"),
                title = o.optString("title"),
                status = o.optString("status"),
                running = o.optBoolean("running"),
                // 进度字段**两种形状**：漫画任务的 progress 用 `done`，
                // 小说任务（爬虫写的那份）用 `completed` —— 网页端一直是按
                // `isManga ? p.done : p.completed` 取的（templates/tasks.html），
                // Android 侧此前只读 `done` → **小说任务进度永远 0/总数**、
                // 进度条卡在 0%（实测 /api/tasks 的小说任务 progress 里只有 completed）。
                done = if (isMangaTask) p.optInt("done") else p.optInt("completed", p.optInt("done")),
                total = p.optInt("total"),
                current = p.optString("current"),
                speed = p.optDouble("speed", 0.0),
                eta = p.optDouble("eta", 0.0),
                error = o.optString("error"),
                mangaKey = o.optString("manga_key"),
                stopKind = o.optString("stop_kind"),
                stopReason = o.optString("stop_reason"),
                resumableFlag = o.optBoolean("resumable", false),
                checkpointDone = o.optJSONObject("checkpoint")?.optInt("done") ?: 0,
                checkpointTotal = o.optJSONObject("checkpoint")?.optInt("total") ?: 0,
                checkpointFailed = o.optJSONObject("checkpoint")?.optInt("failed") ?: 0,
                imagesDone = p.optInt("images_done"),
                imagesTotal = p.optInt("images_total"),
            )
        }
    }

    /** 备份范围（GET /api/backup/scope）；解析失败返回 null，界面就不显示数字而不是显示 0 */
    fun backupScope(body: String): BackupScope? {
        val o = runCatching { JSONObject(body) }.getOrNull() ?: return null
        val inc = o.optJSONArray("include") ?: return null
        val exc = o.optJSONArray("exclude") ?: return null
        val detail = o.optJSONObject("include_detail") ?: JSONObject()
        val src = detail.optJSONObject("sources") ?: JSONObject()
        val exDetail = o.optJSONObject("exclude_detail") ?: JSONObject()
        return BackupScope(
            includes = (0 until inc.length()).mapNotNull { inc.optJSONObject(it)?.optString("what") },
            excludes = (0 until exc.length()).mapNotNull { i ->
                val e = exc.optJSONObject(i) ?: return@mapNotNull null
                Triple(e.optString("what"), exDetail.optLong(e.optString("category")),
                    e.optString("why"))
            },
            srcFiles = src.optInt("files"), srcBytes = src.optLong("bytes"),
            progressFiles = (0 until (detail.optJSONArray("progress_files")?.length() ?: 0))
                .mapNotNull { i ->
                    val p = detail.optJSONArray("progress_files")?.optJSONObject(i)
                        ?: return@mapNotNull null
                    p.optString("name") to p.optLong("bytes")
                },
            includeBytes = detail.optLong("bytes"),
            excludedBytes = o.optLong("excluded_total_bytes"),
            note = o.optString("note"),
        )
    }

    /**
     * 存储占用（GET /api/storage）。解析失败返回 null，由界面显示可读错误，
     * 不用"0 字节"假装统计成功。
     */
    fun storageUsage(body: String): StorageUsage? {
        val o = runCatching { JSONObject(body) }.getOrNull() ?: return null
        if (!o.has("total_bytes")) return null
        fun cats(): List<StorageCategory> {
            val arr = o.optJSONArray("categories") ?: return emptyList()
            return (0 until arr.length()).mapNotNull { i ->
                val c = arr.optJSONObject(i) ?: return@mapNotNull null
                StorageCategory(
                    key = c.optString("key"), label = c.optString("label"),
                    kind = c.optString("kind"), regenerable = c.optBoolean("regenerable"),
                    note = c.optString("note"), bytes = c.optLong("bytes"),
                    count = c.optInt("count"),
                )
            }
        }
        val books = (0 until (o.optJSONArray("novel_books")?.length() ?: 0)).mapNotNull { i ->
            val b = o.optJSONArray("novel_books")?.optJSONObject(i) ?: return@mapNotNull null
            val idx = b.optJSONArray("cached_indexes")
            StorageBook(
                key = b.optString("key"), name = b.optString("name"),
                author = b.optString("author"), total = b.optInt("total"),
                cached = b.optInt("cached"),
                cachedIndexes = (0 until (idx?.length() ?: 0)).mapNotNull { idx?.optInt(it) },
                failed = b.optInt("failed"), bytes = b.optLong("bytes"),
                stopReason = b.optString("stop_reason"),
            )
        }
        val md = (0 until (o.optJSONArray("manga_downloads")?.length() ?: 0)).mapNotNull { i ->
            val m = o.optJSONArray("manga_downloads")?.optJSONObject(i) ?: return@mapNotNull null
            StorageManga(m.optString("source"), m.optString("comic_id"), m.optString("title"),
                m.optInt("images"), m.optLong("bytes"))
        }
        val mc = (0 until (o.optJSONArray("manga_cache")?.length() ?: 0)).mapNotNull { i ->
            val m = o.optJSONArray("manga_cache")?.optJSONObject(i) ?: return@mapNotNull null
            StorageMangaCache(m.optString("source"), m.optInt("works"), m.optLong("bytes"))
        }
        val tr = (0 until (o.optJSONArray("trash")?.length() ?: 0)).mapNotNull { i ->
            val t = o.optJSONArray("trash")?.optJSONObject(i) ?: return@mapNotNull null
            StorageTrashItem(t.optString("name"), t.optLong("bytes"))
        }
        return StorageUsage(
            totalBytes = o.optLong("total_bytes"), categories = cats(),
            novelBooks = books, mangaDownloads = md, mangaCache = mc,
            trashItems = tr, generatedAt = o.optString("generated_at"),
        )
    }

    /** 逐源功能验证结果（含每阶段证据；缺失字段按 0，不用推测填充） */
    fun verifications(body: String): List<SourceVerification> {
        val o = runCatching { JSONObject(body) }.getOrNull() ?: return emptyList()
        val arr = o.optJSONArray("items") ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            val it = arr.optJSONObject(i) ?: return@mapNotNull null
            val st = it.optJSONObject("stages") ?: JSONObject()
            SourceVerification(
                uid = it.optString("uid"),
                name = it.optString("name"),
                status = it.optString("status", "unknown"),
                reason = it.optString("reason"),
                testedAt = it.optString("tested_at"),
                searchCount = st.optJSONObject("search")?.optInt("count") ?: 0,
                tocCount = st.optJSONObject("toc")?.optInt("count") ?: 0,
                contentChars = st.optJSONObject("content")?.optInt("chars") ?: 0,
                note = it.optString("note"),
            )
        }
    }

    /** 逐源验证汇总：通过率的分母是**当前启用源总数**，未验证的单独列出来 */
    data class VerifySummary(
        val enabledTotal: Int,
        val allTotal: Int,
        val verifiedEnabled: Int,
        val recorded: Int,
        val verified: Int,
        val partial: Int,
        val failed: Int,
        val unsupported: Int,
        val neverAttempted: Int,
        val neverAttemptedAll: Int,
        val passRate: Int,
        val passRateAll: Int,
    ) {
        /** 启用口径：我开着的源里有多少能用 */
        val label: String
            get() = "启用源通过率 $passRate%（通过 $verifiedEnabled / 启用 $enabledTotal" +
                (if (neverAttempted > 0) "，未验证 $neverAttempted" else "") + "）"

        /** 全量口径：整包内置源的整体可用度（含停用源） */
        val labelAll: String
            get() = "全部源 $passRateAll%（通过 $verified / 共 $allTotal" +
                (if (neverAttemptedAll > 0) "，未验证 $neverAttemptedAll" else "") + "）"
    }

    fun verifySummary(body: String): VerifySummary? {
        val sm = runCatching { JSONObject(body).optJSONObject("summary") }.getOrNull() ?: return null
        return VerifySummary(
            enabledTotal = sm.optInt("enabled_total"),
            allTotal = sm.optInt("all_total"),
            verifiedEnabled = sm.optInt("verified_enabled"),
            recorded = sm.optInt("recorded"),
            verified = sm.optInt("verified"),
            partial = sm.optInt("partial"),
            failed = sm.optInt("failed"),
            unsupported = sm.optInt("unsupported"),
            neverAttempted = sm.optInt("never_attempted"),
            neverAttemptedAll = sm.optInt("never_attempted_all"),
            passRate = sm.optInt("pass_rate"),
            passRateAll = sm.optInt("pass_rate_all"),
        )
    }

    /** 验证批次信息（时间/关键词/汇总计数），界面用于说明"这是哪一轮的结果" */
    fun verifyMeta(body: String): Pair<String, String> {
        val o = runCatching { JSONObject(body) }.getOrNull() ?: return "" to ""
        return o.optString("tested_at") to o.optString("keyword")
    }

    /** 小说搜索（GET /api/search?q=）：按"书"分组，组内是各源的具体条目 */
    fun novelSearch(body: String): Pair<List<NovelSearchGroup>, SearchMeta> {
        val o = runCatching { JSONObject(body) }.getOrNull() ?: return emptyList<NovelSearchGroup>() to SearchMeta()
        val arr = o.optJSONArray("groups")
        val groups = (0 until (arr?.length() ?: 0)).mapNotNull { i ->
            val g = arr!!.optJSONObject(i) ?: return@mapNotNull null
            val sa = g.optJSONArray("sources")
            val srcs = (0 until (sa?.length() ?: 0)).mapNotNull { j ->
                val x = sa!!.optJSONObject(j) ?: return@mapNotNull null
                NovelSearchSource(
                    sourceUid = x.optString("source_uid"),
                    sourceName = x.optString("source_name"),
                    bookUrl = x.optString("book_url"),
                    lastChapter = x.optString("last_chapter"),
                    chapterCount = x.optInt("chapter_count"),
                    wordCount = x.optString("word_count"),
                )
            }
            NovelSearchGroup(
                name = g.optString("name"),
                author = g.optString("author"),
                intro = g.optString("intro"),
                cover = g.optString("cover"),
                sources = srcs,
            )
        }
        val meta = SearchMeta(
            total = o.optInt("total", groups.size),
            partial = o.optBoolean("partial"),
            timedOutSources = o.optInt("timed_out_sources"),
            networkDown = o.optBoolean("network_down"),
        )
        return groups to meta
    }

    /** 漫画搜索（GET /api/manga/search?q=）：结果 + 失败源原因（errors 不是"没结果"） */
    fun mangaSearch(body: String): Pair<List<MangaSearchHit>, SearchMeta> {
        val o = runCatching { JSONObject(body) }.getOrNull() ?: return emptyList<MangaSearchHit>() to SearchMeta()
        val arr = o.optJSONArray("results")
        val hits = ArrayList<MangaSearchHit>()
        for (i in 0 until (arr?.length() ?: 0)) {
            val r = arr!!.optJSONObject(i) ?: continue
            val tagsArr = r.optJSONArray("tags")
            val tags = (0 until (tagsArr?.length() ?: 0)).mapNotNull { tagsArr?.optString(it) }
            val title = r.optString("title")
            val author = r.optString("author")
            val cover = r.optString("cover")
            val sa = r.optJSONArray("sources")
            if (sa == null) {
                // 兼容顶层就是条目的旧形态（服务端曾有过这种实现），不猜、按字段读
                hits.add(MangaSearchHit(r.optString("source"), r.optString("source_name"),
                    r.optString("id"), title, author, cover, tags))
                continue
            }
            for (j in 0 until sa.length()) {
                val x = sa.optJSONObject(j) ?: continue
                hits.add(MangaSearchHit(
                    source = x.optString("source"),
                    sourceName = x.optString("source_name"),
                    comicId = x.optString("id"),
                    title = title, author = author, cover = cover, tags = tags,
                ))
            }
        }
        val errs = LinkedHashMap<String, String>()
        o.optJSONObject("errors")?.let { e ->
            e.keys().asSequence().toList().forEach { k -> errs[k] = e.optString(k) }
        }
        val meta = SearchMeta(
            total = hits.size,
            partial = errs.isNotEmpty(),
            errors = errs,
            hasMore = o.optBoolean("has_more"),
            page = o.optInt("page", 1),
            networkDown = o.optBoolean("network_down"),
        )
        return hits to meta
    }

    /** 书库批量检查更新：运行状态 + 逐部结果（标题 + 单部检查结果） */
    fun mangaLibraryUpdate(body: String): MangaLibraryUpdate {
        val o = runCatching { JSONObject(body) }.getOrNull()
            ?: return MangaLibraryUpdate(false, 0, 0, emptyList())
        val arr = o.optJSONArray("results")
        val items = (0 until (arr?.length() ?: 0)).mapNotNull { i ->
            val it = arr!!.optJSONObject(i) ?: return@mapNotNull null
            val title = it.optString("title").ifBlank {
                it.optString("comic_id").ifBlank { it.optString("source") }
            }
            title to mangaUpdateCheck(it.toString())
        }
        return MangaLibraryUpdate(
            running = o.optBoolean("running"),
            done = o.optInt("done", items.size),
            total = o.optInt("total", items.size),
            items = items,
        )
    }

    /** 漫画更新检查结果（字段缺失按未知处理，不把失败当"已是最新"） */
    fun mangaUpdateCheck(body: String): MangaUpdateCheck {
        val o = runCatching { JSONObject(body) }.getOrNull()
            ?: return MangaUpdateCheck(false, false, 0, "", "响应无法解析", emptyList())
        val err = o.optString("error")
        val ok = o.optBoolean("ok", err.isBlank())
        val arr = o.optJSONArray("missing")
        val ids = (0 until (arr?.length() ?: 0)).mapNotNull { i ->
            val it = arr!!.optJSONObject(i) ?: run {
                val s = arr.optString(i); if (s.isBlank()) null else s
            }
            if (it is JSONObject) (it.optString("id").ifBlank { it.optString("chapter_id") })
            else it as? String
        }.filter { it.isNotBlank() }
        return MangaUpdateCheck(
            ok = ok,
            hasUpdate = o.optBoolean("has_update"),
            missingCount = o.optInt("missing_count", ids.size),
            latest = o.optString("latest"),
            error = err,
            missingIds = ids,
        )
    }

    /** 探索接口里关于漫画侧的说明（按适配器实况计算；不支持时必须给出原因） */
    fun exploreMangaNote(body: String): Pair<Boolean, String> {
        val o = runCatching { JSONObject(body).optJSONObject("manga") }.getOrNull()
            ?: return false to ""
        return o.optBoolean("supported") to o.optString("reason")
    }

    /** 漫画侧探索源（实现了排行/分类的适配器） */
    data class MangaBrowseSource(val key: String, val name: String,
                                 val categories: List<ExploreCategory>)

    fun exploreMangaSources(body: String): List<MangaBrowseSource> {
        val o = runCatching { JSONObject(body).optJSONObject("manga") }.getOrNull()
            ?: return emptyList()
        val arr = o.optJSONArray("sources") ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            val s = arr.optJSONObject(i) ?: return@mapNotNull null
            val ca = s.optJSONArray("categories")
            MangaBrowseSource(
                key = s.optString("key"), name = s.optString("name"),
                categories = (0 until (ca?.length() ?: 0)).mapNotNull { j ->
                    val c = ca!!.optJSONObject(j) ?: return@mapNotNull null
                    // group：适配器自己声明的分组（如「排行/分类」），原样显示；
                    // 界面上的分组必须来自源本身，不替它编组。
                    ExploreCategory(c.optString("name"), c.optString("group"),
                                    c.optString("key"))
                },
            )
        }
    }

    /** 探索源清单（GET /api/explore/sources）：只含真有榜单/分类的源 */
    fun exploreSources(body: String): Pair<List<ExploreSource>, Pair<Int, Int>> {
        val o = runCatching { JSONObject(body) }.getOrNull()
            ?: return emptyList<ExploreSource>() to (0 to 0)
        val arr = o.optJSONArray("sources")
        val out = (0 until (arr?.length() ?: 0)).mapNotNull { i ->
            val src = arr!!.optJSONObject(i) ?: return@mapNotNull null
            val ca = src.optJSONArray("categories")
            ExploreSource(
                uid = src.optString("uid"),
                name = src.optString("name"),
                enabled = src.optBoolean("enabled", true),
                categories = (0 until (ca?.length() ?: 0)).mapNotNull { j ->
                    val c = ca!!.optJSONObject(j) ?: return@mapNotNull null
                    ExploreCategory(c.optString("title"), c.optString("group"), c.optString("url"))
                },
            )
        }
        return out to (o.optInt("total_sources") to o.optInt("with_explore"))
    }

    /** 探索书单（GET /api/explore?source=&url=&page=） */
    fun exploreBooks(body: String): List<ExploreBook> {
        val arr = runCatching { JSONObject(body).optJSONArray("books") }.getOrNull()
            ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            val b = arr.optJSONObject(i) ?: return@mapNotNull null
            ExploreBook(
                name = b.optString("name"),
                author = b.optString("author"),
                bookUrl = b.optString("book_url"),
                cover = b.optString("cover"),
                sourceUid = b.optString("source_uid"),
                sourceName = b.optString("source_name"),
                lastChapter = b.optString("last_chapter"),
            )
        }
    }

    /** 引擎自检摘要（/__mobile/status） */
    fun engineSummary(body: String): Triple<String, String, List<String>> {
        val o = runCatching { JSONObject(body) }.getOrNull() ?: return Triple("?", "?", emptyList())
        val rt = o.optJSONObject("runtime")
        val caps = o.optJSONObject("capabilities")?.optJSONArray("missing")
        val missing = (0 until (caps?.length() ?: 0)).map { caps!!.optString(it) }
        return Triple(
            rt?.optString("state") ?: "?",
            rt?.optString("wsgi") ?: "?",
            missing,
        )
    }
}
