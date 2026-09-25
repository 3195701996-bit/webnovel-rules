# -*- coding: utf-8 -*-
"""漫画下载管理器（对齐 venera LocalManager + ImagesDownloadTask）
- 任务队列：多任务排队，全局并发上限
- 状态机：queued → running → paused/stopped/done/error
- 暂停/继续（当前章节完成后暂停）、取消（删除任务）
- 章节选择下载（chapters 参数）+ 分组信息
- 图片级进度/速度/ETA + 持久化（重启后恢复）
"""
import copy
import json
import os
import threading
import time
import traceback

from .base import MangaError  # R67: 必须模块级导入——函数内局部 import 在
# 走缓存章节路径时不执行，538 行 raise MangaError 会抛 UnboundLocalError，
# 把所有图片获取失败误记成章节失败（实测渣女沒渣報 34 章全灭）
from ..app_utils import atomic_write  # P2-2: _tasks.json 原子写单一事实源


# P1-1: 结构化停止原因（与 server/state.py 的 STOP_KIND_* 取值**逐字一致**：
# 同一个界面/同一个 /api/tasks 渲染两条后端。engine 不得 import server，
# 故此处独立定义，并由 tests/test_task_stop_reason.py 断言两边取值相同）
STOP_KIND_USER_PAUSE = "user_pause"
STOP_KIND_USER_STOP = "user_stop"
STOP_KIND_SERVICE = "service_stopped"
STOP_KIND_RESTART = "process_restart"
STOP_KIND_ERROR = "error"
STOP_KIND_DONE = "done"

MAX_PARALLEL = 2          # 全局并发下载任务数（可配置）
SAVE_EVERY_CHAPTERS = 3   # 每 N 章持久化一次
# 图片并发按源分级（2026-09-16 实测：jm CDN 对并行是真的并行，阅读预热 8 路
# 长期无恙；jm 下载取 6）。拷贝漫画有 IP 级 210 软限制前科 → 单独保守档 4。
IMG_PARALLEL = 6          # 单任务图片并发下载数（同章内，默认档）
def _img_parallel_for(source):
    return 4 if source in ("copymanga", "copymanga_web") else IMG_PARALLEL
# 图片下载节流（仅拷贝）：原 3s/图（持锁睡觉，20 图一话至少 60s，是"下载过慢"
# 主因）。图片走 CDN 而非 API 通道；1s/图 = 60 次/分，较原 40 次/分仅小幅上调，
# 仍远低于阅读通道的突发并发。
IMG_MIN_INTERVAL = 1.0    # 拷贝下载每张图最小间隔秒（其它源不节流）
# P1-1: 章节图片列表解析钩子（server/state.py 启动时注入 _get_chapter_images，
# 下载 worker 走与阅读通道统一的三级缓存；None 时直连适配器，保持 engine 独立可用）
images_resolver = None
# B03: 书库统计增量更新钩子（server/state.py 注入 _manga_stats_note_change；
# 下载完成写库后回调 (source, comic_id) 重扫该部快照。None 时仅写库，
# 保持 engine 独立可用）
library_change_hook = None
import re as _re


def _sort_chapters(chapters):
    """按章节绝对顺序排序（第X话升序；卷X话按话号主、卷号次；
    附加类（带文字但无话号的休載公告/特別篇/贺图/番外等）排正片之后；
    整卷条目排正片后、附加前；无数字章节排最后）
    修复：'特別篇2'/'休載公告1' 等带文字的章节被兜底数字误当话号插入正片中间"""
    def _num_key(ch):
        name = ch.get("name") or ""
        # 话号（第X话/話/回/章/集；繁简都要识别，否则"第01話"被误判为附加）
        m = _re.search(r"第\s*(\d+(?:\.\d+)?)\s*(?:话|話|回|章|集|话数|話数)", name)
        ep = float(m.group(1)) if m else None
        # 卷号（第X卷/Vol.X/卷X）
        m2 = _re.search(r"(?:第\s*(\d+(?:\.\d+)?)\s*卷|Vol\.?\s*(\d+(?:\.\d+)?)|卷\s*(\d+(?:\.\d+)?))", name, _re.I)
        vol = float(m2.group(1) or m2.group(2) or m2.group(3)) if m2 else 0.0
        if ep is None:
            if m2:
                # 整卷条目：无话号有卷号 → 排正片之后、附加之前
                return [1, 1e12, vol, name]
            # 纯数字名（如 "1" / "1.5" / "12"）→ 当正片（部分源无"第X话"前缀）
            m3 = _re.match(r"^\s*(\d+(?:\.\d+)?)\s*$", name)
            if m3:
                return [0, float(m3.group(1)), 0.0, name]
            # 带文字且无话号（休載公告/特別篇/番外/贺图…）→ 附加，排正片之后
            return [1, 1e12, 1e12, name]
        return [0, ep, vol, name]
    return sorted(chapters, key=_num_key)


# 整卷合集识别：卷号支持阿拉伯数字与中文数字（第一卷/第十二卷），
# 卷字支持中文"卷"与日文"巻"，另含 Vol./Volume 与"单行本"前缀。
# R36: 原实现只认阿拉伯数字，"第一卷/第二卷"这类中文卷号会被当成正片下载。
_VOL_NUM = r"(?:\d+|[一二三四五六七八九十百零〇廿卅]+)"
_VOL_ONLY_RE = _re.compile(
    r"(?:單行本|单行本)?\s*"
    r"(?:"
    r"第\s*" + _VOL_NUM + r"\s*[卷巻]"      # 第01卷 / 第一卷 / 第1巻
    r"|[卷巻]\s*" + _VOL_NUM +              # 卷1 / 巻一
    r"|" + _VOL_NUM + r"\s*[卷巻]"          # 1卷 / 一卷
    r"|Vol(?:ume)?\.?\s*\d+"                # Vol.1 / Volume 1
    r")\s*",
    _re.I)


def _is_volume_only(name):
    """判断是否为**纯整卷合集**章节（整名就是"第X卷/Vol.X/卷X/X卷"）。
    - True → 整卷合集，chapter2 接口返回 null 不可下载，需排除
    - False → 普通章节或"01卷番外/02卷宣傳圖"等卷附加内容（可下载）
    注意：必须整名匹配——"01卷番外"含"01卷"但不是整卷，不能误伤"""
    n = (name or "").strip()
    if not n:
        return False
    return bool(_VOL_ONLY_RE.fullmatch(n))

def filter_volume_only(chapters, sel_chapters=None):
    """剔除"纯整卷合集"章节 → (最终章节列表, 说明文本)。

    规则本体沿用 R36（整名匹配"第N卷/Vol.N/第一卷"，那是 APP 的 chapter2 接口
    返回 null 的打包条目）。2026-09-18 补两条**例外**，实测《巨人》(jurenmeiman)
    的 5 章全叫"第01卷…第05卷"，按规则全被剔除 → 整本下载 total=0 → 任务 error，
    **该作品完全下载不了**（而网页通道实测这些卷能取到 24 张图）：

      · **用户明确选了话 → 不剔除**：他点了就是要下，选择优先于自动规则；
      · **剔完一章不剩 → 不剔除**：宁可按"卷就是可下载单元"处理，
        也不要把整本变成不可下载（真要下不了，逐章失败会如实记账）。

    混合列表仍按原规则剔除（保护 R36 的初衷：别把时间花在打包条目上）。
    """
    chs = list(chapters or [])
    sel = bool(sel_chapters)
    kept = [c for c in chs if not _is_volume_only(c.get("name") or "")]
    if not chs:
        return chs, ""
    if sel:
        return chs, "用户已明确选择章节，不做整卷排除"
    if not kept:
        return chs, ("该作品 %d 章在源站全部是整卷条目（如“第01卷”）："
                     "不再按整卷规则排除（这些卷就是可下载单元）" % len(chs))
    if len(kept) != len(chs):
        return kept, "按整卷规则跳过 %d 个整卷合集条目" % (len(chs) - len(kept))
    return kept, ""


_ADAPTERS_LOADED = False
_ADAPTERS_LOCK = threading.Lock()


def _ensure_adapters():
    """确保适配器注册表已加载（下载 worker 独立线程上下文）"""
    global _ADAPTERS_LOADED
    if _ADAPTERS_LOADED:
        return
    with _ADAPTERS_LOCK:
        if _ADAPTERS_LOADED:
            return
        try:
            from .copymanga import CopyManga
            from .copymanga_web import CopyMangaWeb
            from .mangadex import MangaDex
            from .baozi import Baozi
            from .jm import Jm
            from .nhentai import Nhentai
            from .manager import register_adapter
            # 拷贝漫画优先注册网页版（更稳定，APP API 仅作兜底）
            for cls in (CopyMangaWeb, CopyManga, MangaDex, Baozi, Jm, Nhentai):
                register_adapter(cls)
            _ADAPTERS_LOADED = True
        except Exception as e:
            print(f"[manga-dl] 适配器注册失败: {e}", flush=True)


class DownloadManager:
    def __init__(self, state_file=None):
        self._tasks = {}          # key -> task dict
        self._lock = threading.Lock()
        self._state_file = state_file
        # 进度落盘节流时间戳（见 _save_throttled）
        self._last_save_at = 0.0
        self._queue_cond = threading.Condition(self._lock)
        self._active = 0          # 当前并行任务数
        self._threads = {}        # key -> worker thread
        # P1-2: 存活 worker 的 key 集合（分配槽位时登记、worker finally 注销）。
        # 保证同一 key 任何时刻最多一个活 worker——cancel/delete 后任务 dict
        # 可能已被替换/删除，但旧 worker 还没到检查点，此时禁止重建同 key 任务
        self._worker_alive = set()
        self._save_io_lock = threading.Lock()
        self._save_seq = 0
        self._save_written = 0

    # ── 持久化 ──
    def save(self):
        if not self._state_file:
            return
        try:
            with self._lock:
                # P2-1: 深快照——dict(v) 浅拷贝会让嵌套结构（chapters/current 等）
                # 与 worker 后续的原地修改共享引用；写盘在 _lock 外进行，序列化
                # 到中途被改会写出自相矛盾/半新半旧的记录。deepcopy 在锁内完成，
                # 写盘仍按 _save_seq 丢弃过期快照。
                data = {k: copy.deepcopy(v) for k, v in self._tasks.items()
                        if v.get("status") in ("running", "done", "error",
                                               "stopped", "cancel", "queued", "paused")}
                self._save_seq += 1
                _seq = self._save_seq
            # P2-2: 原子写（tmp+fsync+os.replace）——此前 open(w) 截断写，
            # 崩溃会留下半截 JSON，load() 解析失败即全量任务记录丢失。
            # 快照在 _lock 内、写盘在 _lock 外（不让 fsync 阻塞任务状态读写），
            # 但写盘经 _save_io_lock 串行且按快照序号丢弃过期快照——否则
            # 先取快照、后落盘的线程会用旧进度覆盖新进度
            with self._save_io_lock:
                if _seq < self._save_written:
                    return
                atomic_write(self._state_file, data)
                self._save_written = _seq
        except Exception as e:
            print(f"[manga-dl] 持久化失败: {e}", flush=True)

    def _save_throttled(self, min_interval=5.0):
        """下载过程中把进度**定期落盘**（章末仍照旧 save）。

        为什么需要：进度此前只在**章末**落盘，单章任务（或一章几十张的慢源）在
        系统强杀/断电/升级后，磁盘上的记录还停在 0/0——任务虽然能装载、能续传，
        但用户看到的进度是 0，像"根本没下过"（实测：强杀后本地已有 40 张，
        记录里 images_done 仍是 0/0）。
        调用点必须在**释放 self._lock 之后**（save() 内部会再取同一把非重入锁）。
        """
        now = time.time()
        with self._lock:
            if now - self._last_save_at < min_interval:
                return
            self._last_save_at = now
        self.save()

    def load(self):
        """启动时装载磁盘上的任务记录（running → stopped，可点继续）。

        加载过程**必须留日志**：任务装载是"重启/强杀/升级后任务还在不在"的唯一证据，
        静默返回（此前既没日志也看不出装载了没）让排查只能靠猜
        （实测：设备上任务没被装载，日志里一点痕迹都没有）。
        """
        if not self._state_file:
            print("[manga-dl] 未配置任务状态文件，跳过装载", flush=True)
            return
        if not os.path.exists(self._state_file):
            print(f"[manga-dl] 任务状态文件不存在，跳过装载: {self._state_file}",
                  flush=True)
            return
        try:
            with open(self._state_file, encoding="utf-8") as _f:
                data = json.load(_f)
            if not isinstance(data, dict):
                print(f"[manga-dl] 任务状态文件格式异常（{type(data).__name__}），跳过装载",
                      flush=True)
                return
            n = 0
            with self._lock:
                for key, job in data.items():
                    if not isinstance(job, dict):
                        continue
                    if job.get("status") == "running":
                        job["status"] = "stopped"  # 重启后线程消失
                        job["current"] = ""
                        # P1-1: 记录原因（界面照实显示"为什么停了"，可继续）
                        job["stop_kind"] = STOP_KIND_RESTART
                        job["stop_reason"] = ("应用进程或前台服务已停止，下载随之中断；"
                                              "已下载的图片保留，可点「继续」接着下。")
                    self._tasks[key] = job
                    n += 1
            print(f"[manga-dl] 已装载 {n} 个任务记录（{self._state_file}）", flush=True)
        except Exception as e:
            print(f"[manga-dl] 加载失败: {e}", flush=True)

    # ── 任务管理 ──
    def start(self, source, comic_id, title, chapters=None, cover=""):
        """启动/排队下载（幂等：R11 Kimi 加固）。
        - running：直接返回
        - queued/paused：合并新章节、不丢进度、恢复排队
        - stopped/error/done：重置进度重启（保留封面）"""
        key = f"{source}:{comic_id}"
        _kick = False
        with self._lock:
            exist = self._tasks.get(key)
            if exist:
                status = exist.get("status")
                if status == "running":
                    return key, "running"
                if status in ("queued", "paused"):
                    if chapters:
                        old_ids = {c.get("id") for c in exist.get("chapters", [])
                                   if isinstance(c, dict) and "id" in c}
                        for c in chapters:
                            cid = c.get("id") if isinstance(c, dict) else c
                            if cid not in old_ids:
                                exist.setdefault("chapters", []).append(c)
                    exist["paused"] = False
                    exist["stop_kind"] = ""    # 重新跑起来了，旧停止原因作废
                    exist["stop_reason"] = ""
                    if status == "paused":
                        exist["status"] = "queued"
                        _kick = True
                    _need_save = True  # save() 在锁外调用（内部会再取锁，防死锁）
                    _cur_status = exist["status"]
                else:
                    # P1-2: 旧 worker 尚未退出（cancel 后还没到检查点）时拒绝重建。
                    # 否则旧 worker 的 _checkpoint 读到的是新 dict（非 cancel）
                    # → 两 worker 并发下同书；且旧 worker 退出时 _mark_done 会把
                    # 新任务提前置终态。返回 "stopping"：API 只透传 status 字符串，
                    # 前端轮询会看到任务真实状态（cancel→stopped），稍后重试即可
                    if key in self._worker_alive:
                        return key, "stopping"
                    cover = cover or exist.get("cover", "")
                    _need_save = True
                    _cur_status = "queued"
                    self._tasks[key] = {
                        "status": "queued", "total": 0, "done": 0, "current": "",
                        "error": "", "title": title, "source": source,
                        "comic_id": comic_id, "cover": cover,
                        "speed": 0, "eta": 0, "images_done": 0, "images_total": 0,
                        "failed_chapters": 0,
                        "started_at": time.time(), "type": "manga",
                        "chapters": chapters or [], "paused": False,
                        "stop_kind": "", "stop_reason": "",
                    }
            else:
                # P1-2: cancel→delete 后立即 restart——任务 dict 已 pop 但旧
                # worker 仍存活（下个检查点才会 gone 退出），同样拒绝重建
                if key in self._worker_alive:
                    return key, "stopping"
                _need_save = True
                _cur_status = "queued"
                self._tasks[key] = {
                    "status": "queued", "total": 0, "done": 0, "current": "",
                    "error": "", "title": title, "source": source,
                    "comic_id": comic_id, "cover": cover,
                    "speed": 0, "eta": 0, "images_done": 0, "images_total": 0,
                    "failed_chapters": 0,
                    "started_at": time.time(), "type": "manga",
                    "chapters": chapters or [], "paused": False,
                    "stop_kind": "", "stop_reason": "",
                }
        if _need_save:
            self.save()
        if _kick:
            self._kick_workers()
        else:
            self._kick_workers()
        return key, _cur_status

    def _kick_workers(self):
        """根据并发上限启动 worker 线程（线程启动移出锁，避免线程内等锁死锁）。
        R10 多任务并发加固：**同源互斥**——同一 source 同时最多 1 个下载任务
        （网页版源共享浏览器单例与风控额度；多任务并发会互相 pkill Chrome 导致卡死）。
        不同源任务仍可并行（MAX_PARALLEL 上限内）。"""
        to_start = []
        with self._lock:
            queued = [k for k, v in self._tasks.items()
                      if v.get("status") == "queued"]
            # 正在运行的源集合（同源互斥）
            running_srcs = {v.get("source") for k, v in self._tasks.items()
                            if v.get("status") == "running"}
            while queued and self._active < MAX_PARALLEL:
                key = queued.pop(0)
                # P1-2: 同 key 旧 worker 仍存活（cancel/delete 后未到检查点）→
                # 跳过，绝不并发双 worker；其 finally 释放槽位时会再次
                # _kick_workers 把这个 queued 任务拉起
                if key in self._worker_alive:
                    continue
                _src = self._tasks[key].get("source")
                if _src in running_srcs:
                    continue  # 同源已在下载 → 排队等待（不并发）
                running_srcs.add(_src)
                self._tasks[key]["status"] = "running"
                self._active += 1
                # 槽位分配即登记 alive（而非等 worker 线程起步）——
                # 消除线程启动窗口内 cancel→delete→restart 的检查盲区
                self._worker_alive.add(key)
                to_start.append(key)
        for key in to_start:
            t = threading.Thread(target=self._worker, args=(key,), daemon=True)
            self._threads[key] = t
            t.start()

    def pause(self, key):
        with self._lock:
            t = self._tasks.get(key)
            if not t:
                return False
            st = t.get("status")
            if st == "running":
                t["paused"] = True
                t["stop_kind"] = STOP_KIND_USER_PAUSE
                t["stop_reason"] = "你在 App 里暂停了下载（已下载的图片保留，可点「继续」）"
                return True
            if st == "queued":
                # R67: 排队中任务直接置 paused——否则点了暂停它仍会被
                # _kick_workers 启动(此前 queued 任务暂停按钮完全无效)
                t["paused"] = True
                t["status"] = "paused"
                t["current"] = "已暂停（排队中）"
                t["stop_kind"] = STOP_KIND_USER_PAUSE
                t["stop_reason"] = "你在 App 里暂停了下载（排队中，尚未开始）"
                return True
        return False

    def resume(self, key):
        with self._lock:
            t = self._tasks.get(key)
            # cancel：用户点了取消但 worker 还没到检查点（旧 worker 存活时
            # 分配器会跳过，等它退出再拉起）——这也是"可继续"的一种，
            # 此前 resume 对它直接返回 True 却什么都不做（界面点「继续」无反应）
            if t and t.get("status") in ("paused", "stopped", "error", "cancel"):
                t["paused"] = False
                t["status"] = "queued"
                t["error"] = ""
                t["stop_kind"] = ""     # 重新开始跑，旧原因作废
                t["stop_reason"] = ""   # 重新开始跑，旧原因作废
                # R35: 重置下载计数——旧 worker 异常残留的 done/images_done/
                # failed_chapters 会让续传任务的进度与书库显示失真
                # （实测收银台之星：失败18章后 resume，计数错乱）
                t["done"] = 0
                t["images_done"] = 0
                t["images_total"] = 0
                t["failed_chapters"] = 0
                t["speed"] = 0
                t["eta"] = 0
                t["current"] = ""
        self._kick_workers()
        return True

    def interrupt_running(self, reason):
        """服务/引擎停止：把运行中/排队中的下载标记为可解释的中断（幂等）。

        与 cancel 的区别是**原因由调用方给出**（例如"系统停止了前台服务"），
        并且不动 paused 语义——重新启动后任务仍是 stopped，用户点「继续」即可。
        不 join worker（关闭流程不能被下载阻塞）。返回被标记的任务数。
        """
        n = 0
        with self._lock:
            for key, t in self._tasks.items():
                if t.get("status") in ("running", "queued"):
                    if t.get("status") == "running":
                        t["status"] = "cancel"   # worker 感知退出（收尾会落 stopped）
                    else:
                        t["status"] = "stopped"
                    t["paused"] = False
                    t["current"] = ""
                    t["stop_kind"] = STOP_KIND_SERVICE
                    t["stop_reason"] = reason
                    n += 1
        if n:
            self.save()
        return n

    def cancel(self, key):
        """停止任务（保留记录为 stopped，可重新启动续传）"""
        with self._lock:
            t = self._tasks.get(key)
            if t:
                t["paused"] = False
                if t.get("status") == "running":
                    t["status"] = "cancel"  # worker 感知退出
                elif t.get("status") in ("queued", "paused"):
                    t["status"] = "stopped"
                t["current"] = ""
                t["stop_kind"] = STOP_KIND_USER_STOP
                t["stop_reason"] = ("你在 App 里取消了这次下载（已下载的图片保留，"
                                    "可点「继续」接着下）")
        self.save()
        return True

    def delete(self, key):
        _need_save = False
        with self._lock:
            t = self._tasks.get(key)
            if t and t.get("status") == "running":
                t["status"] = "cancel"  # 先标记停止
                _need_save = True
            elif t:
                # P1-2 协调：非 running 任务直接 pop。若旧 worker 仍存活
                # （此前已被标记 cancel），它会在下个检查点收到 "gone" 自行
                # 退出，并在 finally 里清理 _worker_alive 与 _active——
                # 此处无需也不能代为递减（会与其 finally 重复释放）
                self._tasks.pop(key, None)
                _need_save = True
        if _need_save:
            self.save()

    def status(self, key):
        with self._lock:
            return dict(self._tasks.get(key, {"status": "idle"}))

    def running_keys(self):
        """进行中（running/queued）任务 key 列表"""
        with self._lock:
            return [k for k, v in self._tasks.items()
                    if v.get("status") in ("running", "queued")]

    def paused_keys(self):
        """可恢复（paused/stopped/error）任务 key 列表"""
        with self._lock:
            return [k for k, v in self._tasks.items()
                    if v.get("status") in ("paused", "stopped", "error")]

    def all_tasks(self):
        with self._lock:
            return {k: dict(v) for k, v in self._tasks.items()}

    def _web_fallback_adapter(self):
        """copymanga APP 接口风控时的网页版降级适配器（每任务复用一次）。

        按章节新建实例会重复初始化会话/指纹，几十章的任务开销明显，
        故缓存在实例上。构造失败返回 None 由调用方处理。
        """
        _wa = getattr(self, "_web_fb", None)
        if _wa is not None:
            return _wa
        try:
            from .manager import get_adapter as _ga
            _wa = _ga("copymanga_web", state_dir=self._get_state_dir())
        except Exception:
            _wa = None
        self._web_fb = _wa
        return _wa

    def _mark_done(self, key, status, **extra):
        """终态落库（不可持锁调用——内部取锁且 save() 在锁外）。
        P1-1: _active 递减与 _kick_workers 已上移到 _worker 的 finally
        （每个退出路径精确执行一次）。本方法可能被同一次执行的多个退出
        分支调用（正常完成/cancel/pause/异常/适配器缺失），递减留在这里
        会重复释放槽位；而 "gone" 分支根本不经本方法，则会漏递减。"""
        with self._lock:
            t = self._tasks.get(key)
            if t:
                t.update(status=status, **extra)
                # P1-1: 终态的结构化原因（与小说任务同字段同取值）。
                # 只写 error/done 两种自身可判定的终态；stopped/paused 的原因
                # 由 pause()/cancel()/interrupt_running() 事先写清，此处不覆盖
                if status == "error":
                    t["stop_kind"] = STOP_KIND_ERROR
                elif status == "done":
                    t["stop_kind"] = STOP_KIND_DONE
                if status in ("done", "error", "stopped", "cancel"):
                    t["current"] = ""
                    t["finished_at"] = time.time()
        self.save()

    # ── worker：实际下载 ──
    def _checkpoint(self, key):
        """暂停/取消检查点：返回 "pause"/"cancel"/None。
        R67: worker 的每个长循环(主章节循环/失败复查/补页重试)都必须
        周期性调用——此前只在主循环开头检查，复查阶段(每章最多 3 次
        源站渲染取图)完全无检查点 → 暂停请求几小时不生效"""
        with self._lock:
            t = self._tasks.get(key)
            if t is None:
                return "gone"
            if t.get("status") == "cancel":
                return "cancel"
            if t.get("paused") or t.get("status") == "paused":
                return "pause"
        return None

    def _apply_checkpoint(self, key, cp):
        """在长循环退出点应用检查点结果（_mark_done 不可持锁调用）"""
        if cp == "cancel":
            self._mark_done(key, "stopped", error="")
            return True
        if cp == "pause":
            self._mark_done(key, "paused")
            return True
        return False

    def _worker(self, key):
        try:
            print(f"[manga-dl] worker 启动: {key}", flush=True)
            from .downloader import ImageDownloader, BadImageError, StorageWriteError
            from .manager import get_adapter
            _ensure_adapters()
            print(f"[manga-dl] worker {key} 适配器就绪", flush=True)
            with self._lock:
                task = self._tasks.get(key)
                if not task:
                    return
                source = task["source"]
                comic_id = task["comic_id"]
                title = task["title"]
                sel_chapters = list(task.get("chapters") or [])
            # 适配器复用（单例：避免每次下载 new 实例的初始化请求）
            if not hasattr(self, "_adapter_pool"):
                self._adapter_pool = {}
            if source not in self._adapter_pool:
                self._adapter_pool[source] = get_adapter(source, state_dir=self._get_state_dir())
            ad = self._adapter_pool[source]
            if ad is None:
                self._mark_done(key, "error", error="适配器不存在")
                return
            print(f"[manga-dl] worker {key} ad={ad.name if ad else None}", flush=True)
            # 下载图片存独立 downloads 目录（永久保留，与临时缓存 _cache 隔离；
            # 任何缓存清理都不触碰 downloads）
            cache_root = os.path.join(self._get_downloads_root(), source, comic_id)
            dl = ImageDownloader(ad, cache_root,
                                 min_interval=IMG_MIN_INTERVAL if source == "copymanga" else 0.0)
            print(f"[manga-dl] worker {key} downloader 就绪", flush=True)
            try:
                # 1) 获取章节列表
                info_p = os.path.join(cache_root, "_info.json")
                print(f"[manga-dl] worker {key} 章节获取 cache={cache_root}", flush=True)
                chapters = None
                cover = task.get("cover", "")
                # _info.json TTL：1 小时过期后强制重拉（网页降级缓存可能不全，
                # 过期后优先用 APP API 拉全量章节）
                INFO_TTL = 3600
                if os.path.exists(info_p):
                    try:
                        _age = time.time() - os.path.getmtime(info_p)
                        if _age < INFO_TTL:
                            with open(info_p, encoding="utf-8") as _f:
                                info = json.load(_f)
                            _cand = info.get("chapters") or []
                            # R36: TTL 内复用前也校验与目录的重叠——被污染列表
                            # （降级通道 ID 体系）会导致已下载内容全量重下
                            _dir_ids = {n for n in os.listdir(cache_root)
                                        if os.path.isdir(os.path.join(cache_root, n))}
                            if _dir_ids and _cand and \
                                    not any(c.get("id") in _dir_ids for c in _cand):
                                print(f"[manga-dl] worker {key} 缓存章节列表与目录不匹配，"
                                      f"强制重拉", flush=True)
                            else:
                                chapters = _cand
                                cover = info.get("cover") or cover
                    except Exception:
                        pass
                print(f"[manga-dl] worker {key} 缓存章节: {len(chapters) if chapters else '无'}", flush=True)
                if chapters is None:
                    print(f"[manga-dl] worker {key} 拉取源章节…", flush=True)
                    # R37: 用独立变量承接详情对象。原实现在降级成功后仍访问主
                    # 渠道的 `d`，而 `d` 在主渠道抛异常时从未赋值 →
                    # UnboundLocalError，导致"降级明明拿到了章节却仍判失败"。
                    _detail = None
                    try:
                        _detail = ad.comic_info(comic_id)
                        chapters = [{"id": c.id, "name": c.name, "group": c.group}
                                    for c in _detail.chapters]
                    except Exception as _e1:
                        print(f"[manga-dl] worker {key} 源章节失败: {_e1}，尝试降级渠道", flush=True)
                        chapters = None
                        # copymanga 源失败 → 降级 copymanga_web（网页版不受 APP IP 风控）
                        if source == "copymanga":
                            try:
                                from .manager import get_adapter as _ga
                                _web = _ga("copymanga_web", state_dir=self._get_state_dir())
                                if _web:
                                    _detail = _web.comic_info(comic_id)
                                    chapters = [{"id": c.id, "name": c.name, "group": c.group}
                                                for c in _detail.chapters]
                            except Exception as _e2:
                                print(f"[manga-dl] worker {key} 网页降级失败: {_e2}", flush=True)
                        if not chapters:
                            raise MangaError(f"章节获取失败: {_e1}") from _e1
                    cover = (getattr(_detail, "cover", None) or cover)
                    # R36: 章节列表与已下载目录绑定——copymanga APP 与 copymanga_web
                    # 降级通道返回的章节 UUID 体系不同（实测《二人巴士》44章全不匹配），
                    # 直接覆盖 _info.json 会让续传任务把已下载内容全部重下。
                    # 策略：对比新旧列表与磁盘目录的 ID 重叠度，选用重叠最多的列表；
                    # 新列表与目录零重叠且目录非空时，保留旧列表（幂等续传）。
                    _old_chapters = None
                    if os.path.exists(info_p):
                        try:
                            with open(info_p, encoding="utf-8") as _f:
                                _old_info = json.load(_f)
                            _old_chapters = _old_info.get("chapters") or []
                        except Exception:
                            pass
                    _dir_ids = set()
                    try:
                        _dir_ids = {n for n in os.listdir(cache_root)
                                    if os.path.isdir(os.path.join(cache_root, n))}
                    except OSError:
                        pass
                    if _old_chapters:
                        def _overlap(lst):
                            return sum(1 for c in lst if c.get("id") in _dir_ids)
                        _old_ov = _overlap(_old_chapters)
                        _new_ov = _overlap(chapters)
                        if _dir_ids and _new_ov < _old_ov:
                            print(f"[manga-dl] worker {key} 新章节列表与目录重叠 "
                                  f"({_new_ov} vs 旧列表 {_old_ov})，沿用旧列表避免全量重下",
                                  flush=True)
                            chapters = _old_chapters
                    os.makedirs(cache_root, exist_ok=True)
                    # 续传依赖的关键文件：原子写，避免半截 JSON；同时关闭句柄
                    atomic_write(info_p, {"chapters": chapters, "cover": cover,
                                          "title": title})
                # 2) 章节选择
                if sel_chapters:
                    # start() 允许传 {"id": ...} 字典章节，set() 会对 dict 抛 TypeError
                    _sel_set = {c.get("id") if isinstance(c, dict) else c
                                for c in sel_chapters}
                    _filtered = [c for c in chapters if c["id"] in _sel_set]
                    if _filtered:
                        chapters = _filtered
                    else:
                        # 增量传入的章节 id 与源站最新章节全部不匹配
                        # （详情缓存过期/源站章节变动）→ 回退全量下载。
                        # 已下载章节的图片命中本地缓存不会重下（幂等），只补缺失的。
                        print(f"[manga-dl] worker {key} 增量章节 id 均不匹配，回退全量下载 "
                              f"({len(chapters)} 章)", flush=True)
                # 3) 绝对顺序：按章节序号（第X话/卷X话）升序下载（前章节→后章节）
                chapters = _sort_chapters(chapters)
                # 只排除**纯整卷合集**（章节名就是"第X卷/Vol.X/卷X"，APP 的 chapter2
                # 接口返回 null 不可下载）；"01卷番外/02卷宣傳圖"等卷附加内容是实际
                # 可下载章节（实测可取到图片），必须保留。
                #
                # 2026-09-18 两处修正（实测《巨人》jurenmeiman 的 5 章全叫"第0N卷"）：
                #   ① **用户明确选的话不做排除**——他点了就是要下；
                #      旧行为：选了也被滤掉 → total=0 → 任务 error，用户无从理解；
                #   ② 滤完若一章不剩，**不再谎称"源在风控/已下架"**，而是说清真因
                #      （是我们按整卷规则跳过的），并告诉用户下一步怎么做。
                chapters, _vol_note = filter_volume_only(chapters, sel_chapters)
                if _vol_note:
                    print(f"[manga-dl] worker {key} {_vol_note}", flush=True)
                total = len(chapters)
                if total == 0:
                    # 真的没有可下载章节：源风控/下架/目录为空。
                    # （整卷被跳过的情况已由 filter_volume_only 处理，不会走到这里——
                    #   旧实现正是在这里把"我们自己跳过"误报成"源在风控/已下架"。）
                    self._mark_done(key, "error",
                                    error="没有可下载的章节（源可能正在风控或该漫画已下架）")
                    return
                # R68: 封面贯通——任务启动/详情可能没带上封面(历史任务/缓存章节
                # 路径), cover 空时补拉一次详情取封面(仅此一次,任务持久化后
                # 不再重复; 渲染 10s 左右, 相对整本下载可忽略)
                if not cover:
                    try:
                        print(f"[manga-dl] worker {key} 封面为空,补拉详情…", flush=True)
                        _d2 = ad.comic_info(comic_id)
                        cover = getattr(_d2, "cover", "") or cover
                        if cover:
                            print(f"[manga-dl] worker {key} 补拉封面成功", flush=True)
                    except Exception as _ce:
                        print(f"[manga-dl] worker {key} 补拉封面失败: {_ce}", flush=True)
                with self._lock:
                    t = self._tasks.get(key)
                    if t:
                        t["total"] = total
                        t["cover"] = cover or t.get("cover", "")
                self.save()
                done = 0
                images_done = 0
                images_total = 0
                _zero_streak = 0   # 连续零字节章节计数（连续全灭自动暂停用）
                t0 = time.time()
                last_speed_t = t0
                last_done = 0
                # ── 章节流水线：下一话的图片清单与当前话的图片下载重叠 ──
                # 此前每话串行"取清单(0.5-3s 源站往返) → 下图"，整本下来清单
                # 往返占掉可观比例；预取单线程、复用同一条解析路径（含重试与
                # copymanga 网页降级），取消/暂停时最多一个预取在飞。
                import concurrent.futures as _cf2
                _img_ex = _cf2.ThreadPoolExecutor(max_workers=1)
                def _resolve_images(chid):
                    for _try in range(2):
                        try:
                            if images_resolver is not None:
                                return images_resolver(ad, source, comic_id, chid)
                            return ad.images(comic_id, chid)
                        except Exception:
                            if _try < 1:
                                time.sleep(2.0)
                    return None
                def _resolve_images_fb(chid):
                    r = _resolve_images(chid)
                    if r is None and source == "copymanga":
                        try:
                            _wa = self._web_fallback_adapter()
                            if _wa:
                                r = _wa.images(comic_id, chid)
                                print(f"[manga-dl] {chid} 降级网页版取图成功",
                                      flush=True)
                        except Exception:
                            pass
                    return r
                _pre = {"id": None, "fut": None}
                def _kick(chid):
                    if chid and _pre["id"] != chid:
                        _pre["id"] = chid
                        _pre["fut"] = _img_ex.submit(_resolve_images_fb, chid)
                if chapters:
                    _kick(chapters[0]["id"])
                for _ci, ch in enumerate(chapters):
                    # R67: 统一检查点——主循环开头（含 current 预置名称）
                    _cp = self._checkpoint(key)
                    if _cp == "gone":
                        return
                    if _cp:
                        with self._lock:
                            _t = self._tasks.get(key)
                            if _t and _cp == "pause":
                                _t["current"] = f"已暂停（{ch.get('name','')} 后继续）"
                        if self._apply_checkpoint(key, _cp):
                            return
                    try:
                        # 图片清单：优先吃流水线预取（与上一话下载重叠完成），
                        # 未命中则当场解析；拿到本话清单后立刻预取下一话
                        if _pre["id"] == ch["id"] and _pre["fut"] is not None:
                            try:
                                imgs = _pre["fut"].result()
                            except Exception:
                                imgs = None
                        else:
                            imgs = _resolve_images_fb(ch["id"])
                        if _ci + 1 < len(chapters):
                            _kick(chapters[_ci + 1]["id"])
                        if not imgs:
                            # 空列表同样是失败：源站风控/异常响应用"没有 images
                            # 字段"表达错误（jm 的 images() 就是
                            # `[url for img in (j.get('images') or [])]`）。
                            # 当作"0 图成功"会让用户以为下载完了，实际整章没有
                            # 一张图——下载与阅读两处的假成功必须统一成可重试失败。
                            raise MangaError("图片列表为空（源站可能限流）")
                        images_total += len(imgs)
                        with self._lock:
                            t = self._tasks.get(key)
                            if t:
                                t["images_total"] = images_total
                        # 同章图片并发下载（IMG_PARALLEL）
                        from concurrent.futures import ThreadPoolExecutor as _TPE
                        # ch["id"] 用默认参数固化：闭包引用循环变量时，
                        # 只要线程池改为跨章节复用就会把图片写到错误章节目录
                        _ch_id = ch["id"]

                        def _dl_one(item, _cid=_ch_id):
                            _i, _u = item
                            # R67: 单图下载前检查暂停/取消——已被请求时立即跳过
                            # 本图(返回 2)，剩余图快速空转，本章提前结束 →
                            # 主循环下一章检查点即生效，不必等整章几十张下完
                            if self._checkpoint(key):
                                return 2
                            try:
                                dl.get(_u, comic_id, _cid, _i)
                            except BadImageError:
                                return -1    # 源站坏页：永久失败，不计入可续传失败
                            except StorageWriteError:
                                return -2    # 本地写不进去：立刻终止任务并如实报错
                            except Exception:
                                return 0    # 网络/临时失败：可重试
                            # R49h: 单张下载成功即更新内存进度——前端轮询(1.5s)
                            # 实时可见图片级进度;单章几十张不再"几分钟不动"
                            # (只更新内存不落盘,避免小文件高频 IO;章末统一校准)
                            with self._lock:
                                _t = self._tasks.get(key)
                                if _t:
                                    _t["images_done"] = _t.get("images_done", 0) + 1
                                    _t["images_total"] = _t.get("images_total") or                                         _t.get("images_total", 0)
                            # 进度定期落盘（节流）：强杀/断电后恢复出来的进度不能是 0
                            self._save_throttled()
                            return 1
                        with _TPE(max_workers=_img_parallel_for(source)) as _pex:
                            _rets = list(_pex.map(_dl_one, list(enumerate(imgs))))
                        _done_here = sum(1 for r in _rets if r == 1)
                        _bad_here = sum(1 for r in _rets if r == -1)
                        _skip_here = sum(1 for r in _rets if r == 2)
                        _store_here = sum(1 for r in _rets if r == -2)
                        if _store_here:
                            # 本地写不进去（目录不可写/磁盘满）：继续重试其它页毫无意义，
                            # 立刻终止任务并说清原因——否则用户看到的是"一直在下载"
                            _msg = ("本地写入失败：存储不可写或空间不足"
                                    "（本章 %d 张未写入；已下载的内容不受影响）。"
                                    "请清理空间或检查权限后重试" % _store_here)
                            print(f"[manga-dl] {key} {_msg}", flush=True)
                            self._mark_done(key, "error", error=_msg)
                            return
                        images_done += _done_here
                        if _skip_here:
                            # 暂停/取消已在下载途中请求 → 立即退出任务
                            _cp = self._checkpoint(key)
                            if _cp == "gone":
                                return
                            if _cp:
                                with self._lock:
                                    _t = self._tasks.get(key)
                                    if _t and _cp == "pause":
                                        _t["current"] = "已暂停（本页下载完成后停止）"
                                if self._apply_checkpoint(key, _cp):
                                    return
                        if _bad_here:
                            # 源站坏页章节：记录但不计 failed_chapters（R38：
                            # 《反转练习生》5页占位图曾把整任务拖成 error 误导重启）
                            with self._lock:
                                t = self._tasks.get(key)
                                if t:
                                    t.setdefault("bad_page_chapters", 0)
                                    t["bad_page_chapters"] += 1
                                    t.setdefault("bad_page_ids", []).append(_ch_id)
                                    t.setdefault("bad_page_count", 0)
                                    t["bad_page_count"] += _bad_here
                        if _done_here + _bad_here < len(imgs):
                            # 本章有真实失败（网络级）→ 记失败
                            with self._lock:
                                t = self._tasks.get(key)
                                if t:
                                    t.setdefault("failed_chapters", 0)
                                    t["failed_chapters"] += 1
                                    t.setdefault("failed_ids", []).append(_ch_id)
                        # 全灭章节（一张没下到且不是用户暂停）：一律如实记失败——
                        # 风控拦截页会走 BadImageError（"坏页"不计失败）通道漏网，
                        # 表现为"话数在涨、页数不变"的假进度（实测：done 23/87 而
                        # 磁盘只有 7 话）。零字节 = 没下到，无论错误类型。
                        # （与上方"有真实失败"分支互斥，不重复计数）
                        if (_done_here == 0 and _skip_here == 0 and _bad_here
                                and _done_here + _bad_here >= len(imgs)):
                            with self._lock:
                                t = self._tasks.get(key)
                                if t:
                                    t.setdefault("failed_chapters", 0)
                                    t["failed_chapters"] += 1
                                    t.setdefault("failed_ids", []).append(_ch_id)
                        # 连续全灭自动暂停（节流阀）：连续 3 话零字节说明源站大概率
                        # 已风控本 IP，继续跑只会把 87 话全烧成"假完成"。
                        # 暂停并如实写明原因，已下载的保留，恢复后继续。
                        _zero_streak = (_zero_streak + 1
                                        if _done_here == 0 and _skip_here == 0 else 0)
                        if _zero_streak >= 3:
                            _msg = (f"连续 {_zero_streak} 话一张图都没取到"
                                    "（源站可能在风控/限流本机 IP），已自动暂停；"
                                    "已下载的图片保留，稍后点「继续」接着下")
                            print(f"[manga-dl] {key} {_msg}", flush=True)
                            self.pause(key)
                            with self._lock:
                                _t = self._tasks.get(key)
                                if _t:
                                    _t["stop_reason"] = _msg
                            # 等下一话开头的统一检查点退出
                        # 速度/ETA（每次章节后更新，不依赖条件）
                        now = time.time()
                        dt = now - last_speed_t
                        if dt >= 3:
                            rate = (images_done - last_done) / dt if dt > 0 else 0
                            last_speed_t = now
                            last_done = images_done
                            with self._lock:
                                t = self._tasks.get(key)
                                if t:
                                    t["speed"] = round(rate, 1)
                                    rem = max(0, images_total - images_done)
                                    # C06: 速率未知（=0）时 ETA 记 None（前端显示
                                    # "估算中"），不再写 0 被误读为"即将完成"
                                    t["eta"] = int(rem / rate) if rate > 0 else None
                    except Exception as e:
                        print(f"[manga-dl] {ch.get('name')} 失败: {e}", flush=True)
                        # 章节失败：记录失败章节数（不 mark done，保留任务为 error 可重试）
                        with self._lock:
                            t = self._tasks.get(key)
                            if t:
                                t.setdefault("failed_chapters", 0)
                                t["failed_chapters"] += 1
                                t.setdefault("failed_ids", []).append(ch.get("id", ""))
                                t["current"] = f"失败: {ch.get('name','')}"
                    done += 1
                    with self._lock:
                        t = self._tasks.get(key)
                        if t:
                            t["done"] = done
                            t["images_done"] = images_done
                            # C06: 不在章末把 eta 归零——ETA 由上面的速率块统一
                            # 维护；归零会让"剩余时间"在两次采样间闪成 0/消失
                    if done % SAVE_EVERY_CHAPTERS == 0:
                        self.save()
                # 3) 完成：检查是否被取消
                with self._lock:
                    t = self._tasks.get(key)
                    cancelled = bool(t and t.get("status") in ("cancel", "stopped", "paused"))
                    failed = int((t or {}).get("failed_chapters", 0))
                if not cancelled:
                    if failed:
                        # R37: 失败章节磁盘复查——瞬时网络错误后实际已下载完整
                        # 的章节不算失败（"下载完了却显示失败"的根因）：
                        # 复查失败章节的应有图片数与磁盘实际数，完整则剔除
                        _real_fail = []
                        _removed = []
                        with self._lock:
                            _fids = list((t or {}).get("failed_ids") or [])
                        for _fid in _fids:
                            # R67: 复查循环检查点——此前复查无任何暂停检查，
                            # 失败章多时(几十章×3次源站渲染)暂停请求几小时不生效
                            _cp = self._checkpoint(key)
                            if _cp == "gone":
                                return
                            if _cp:
                                with self._lock:
                                    _t = self._tasks.get(key)
                                    if _t:
                                        _t["current"] = "复查失败章节中被暂停/停止"
                                if self._apply_checkpoint(key, _cp):
                                    return
                            try:
                                # R49o: 失败章复查——源站取图多轮重试 + copymanga
                                # 网页降级。210 风控/瞬态返回的 "Chapter not found"
                                # 文案不代表章被移除(实测次日同章恢复正常)，
                                # 一律按可重试失败处理，绝不判"永久移除"
                                _fimgs = None
                                for _try in range(3):
                                    try:
                                        _fimgs = ad.images(comic_id, _fid)
                                        break
                                    except Exception:
                                        _fimgs = None
                                        if _try < 2:
                                            _cp2 = self._checkpoint(key)
                                            if _cp2 and self._apply_checkpoint(key, _cp2):
                                                return
                                            time.sleep(2.0)
                                if _fimgs is None and source == "copymanga":
                                    try:
                                        _wa2 = self._web_fallback_adapter()
                                        if _wa2:
                                            _fimgs = _wa2.images(comic_id, _fid)
                                    except Exception:
                                        _fimgs = None
                                if not _fimgs:
                                    _real_fail.append(_fid)
                                    print(f"[manga-dl] 复查: 章节 {_fid} 源站取图失败"
                                          f"（风控/瞬态），留待重启续传", flush=True)
                                    continue
                                _fdir = os.path.join(cache_root, _fid)
                                _fhave = 0
                                if os.path.isdir(_fdir):
                                    _fhave = sum(1 for fn in os.listdir(_fdir)
                                                 if fn.lower().endswith(
                                                     (".webp", ".jpg", ".png")))
                                if _fhave >= len(_fimgs):
                                    print(f"[manga-dl] 复查: 章节 {_fid} 磁盘完整 "
                                          f"({_fhave}/{len(_fimgs)})，剔除失败", flush=True)
                                    continue
                                # R49o: 源站可取而磁盘缺失 → 复查自动补齐缺失页
                                # （原实现只对比不重下：瞬时缺页把任务永久判 error，
                                # 正是"明明能下却报失败"的根因）
                                print(f"[manga-dl] 复查: 章节 {_fid} 缺页补齐 "
                                      f"({_fhave}/{len(_fimgs)})…", flush=True)
                                for _i in range(len(_fimgs)):
                                    # R67: 补页长循环低频检查点(每 5 页)
                                    if _i > 0 and _i % 5 == 0:
                                        _cpi = self._checkpoint(key)
                                        if _cpi == "gone":
                                            return
                                        if _cpi and self._apply_checkpoint(key, _cpi):
                                            return
                                    _ip = os.path.join(_fdir, f"{_i:04d}")
                                    _has = any(
                                        os.path.isfile(_ip + _e) for _e in
                                        (".webp", ".jpg", ".jpeg", ".png"))
                                    if _has:
                                        continue
                                    for _try in range(2):
                                        try:
                                            dl.get(_fimgs[_i], comic_id, _fid, _i)
                                            break
                                        except BadImageError:
                                            break
                                        except Exception:
                                            if _try == 0:
                                                time.sleep(2.0)
                                _fhave2 = sum(1 for fn in os.listdir(_fdir)
                                              if fn.lower().endswith(
                                                  (".webp", ".jpg", ".png")))
                                if _fhave2 >= len(_fimgs):
                                    print(f"[manga-dl] 复查: 章节 {_fid} 补齐完成 "
                                          f"({_fhave2}/{len(_fimgs)})，剔除失败",
                                          flush=True)
                                    continue
                                _real_fail.append(_fid)
                            except Exception as _re:
                                # R49o: 不按错误文本猜测"永久移除"——一律可重试失败
                                _real_fail.append(_fid)
                                print(f"[manga-dl] 复查: 章节 {_fid} 复查异常: "
                                      f"{type(_re).__name__}: {_re}", flush=True)
                        failed = len(_real_fail)
                        with self._lock:
                            t = self._tasks.get(key)
                            if t:
                                t["failed_chapters"] = failed
                                if _real_fail:
                                    t["failed_ids"] = _real_fail
                                else:
                                    t["failed_ids"] = []
                                if _removed:
                                    t.setdefault("removed_chapters", 0)
                                    t["removed_chapters"] += len(_removed)
                                    t.setdefault("removed_ids", [])
                                    t["removed_ids"] = list(
                                        (t.get("removed_ids") or [])[:0]
                                        + _removed)[:50]
                        if not failed:
                            _note = ""
                            if _removed:
                                _note = (f"{len(_removed)} 章已从源站移除"
                                         f"（Chapter not found），永久缺失非下载遗漏")
                            self._mark_done(key, "done", images_done=images_done,
                                            eta=0)
                            self._write_library(ad, key, title, cover, total,
                                                images_done)
                            if _note:
                                with self._lock:
                                    t = self._tasks.get(key)
                                    if t:
                                        t["note"] = _note
                            print(f"[manga-dl] 复查后任务完成: {key}"
                                  + (f"({_note})" if _note else ""), flush=True)
                        else:
                            _err = f"{failed} 个章节下载失败（源风控/网络），可重新启动续传"
                            if _removed:
                                _err += f"；另有 {len(_removed)} 章已被源站移除"
                            self._mark_done(key, "error", error=_err)
                            self._write_library(ad, key, title, cover, total,
                                                images_done)
                    else:
                        # R38: done 但含源站坏页 → 状态 done + 说明（坏页永久性，
                        # 不再把任务标 error 误导用户反复重启）
                        with self._lock:
                            t = self._tasks.get(key)
                            _bad_n = int((t or {}).get("bad_page_chapters") or 0)
                            _bad_c = int((t or {}).get("bad_page_count") or 0)
                        self._mark_done(key, "done", images_done=images_done, eta=0)
                        self._write_library(ad, key, title, cover, total, images_done)
                        if _bad_n:
                            _note = (f"{_bad_n} 章共 {_bad_c} 页为源站坏页"
                                     f"（占位图/已删除，非下载遗漏）")
                            print(f"[manga-dl] 任务完成(含坏页说明): {key} - {_note}",
                                  flush=True)
                            with self._lock:
                                t = self._tasks.get(key)
                                if t:
                                    t["note"] = _note
                else:
                    self._mark_done(key, "stopped", error="")
            except Exception as e:
                # P2-8: error 字段只存脱敏稳定文案（经 API 回客户端），
                # 完整异常（可能含 URL/内网地址/凭据片段）只进日志
                print(f"[manga-dl] worker {key} 任务异常: "
                      f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
                      flush=True)
                # 磁盘类错误给可行动原因（与小说任务同一归因，见 app_utils）
                from ..app_utils import disk_error_text as _disk_text
                _disk = _disk_text(e)
                self._mark_done(key, "error",
                                error=_disk or "下载过程异常中断，可重新启动续传",
                                stop_reason=_disk or
                                            "下载过程异常中断（已下载的图片保留，可点「继续」）")
        except Exception as e:
            # 启动期异常兜底：主体 try 之前的失败（适配器构造/导入等）
            print(f"[manga-dl] worker {key} 未捕获异常: "
                  f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
                  flush=True)
            from ..app_utils import disk_error_text as _disk_text
            _disk = _disk_text(e)
            self._mark_done(key, "error",
                            error=_disk or "下载过程异常中断，可重新启动续传",
                            stop_reason=_disk or
                                        "下载过程异常中断（已下载的图片保留，可点「继续」）")
        finally:
            # P1-1: _active 槽位释放的唯一位置——正常完成/cancel/pause/
            # gone/异常五条退出路径在此精确递减一次并踢起队列。
            # （递减曾放在 _mark_done："gone" 等 5 处直接 return 的分支
            # 不经 _mark_done → 泄漏；漏 2 次后 _active 卡满 MAX_PARALLEL，
            # 进程余生不再启动任何下载）
            try:
                _img_ex.shutdown(wait=False)      # 流水线预取线程（若在）
            except Exception:
                pass
            with self._lock:
                self._worker_alive.discard(key)
                self._threads.pop(key, None)
                self._active = max(0, self._active - 1)
            self._kick_workers()

    def _write_library(self, ad, key, title, cover, total, images):
        """写入书库记录（data/manga/_library.json）
        R29(技术评审5.3): 全程持锁 + 原子替换——并发任务完成时
        后写入者不再覆盖先写入者的更新"""
        try:
            lib_p = os.path.join(self._get_library_file())
            with self._lock:
                lib = []
                if os.path.exists(lib_p):
                    try:
                        with open(lib_p, encoding="utf-8") as _f:
                            lib = json.load(_f)
                    except Exception:
                        lib = []  # 损坏则重建（原子写后基本不会出现）
                source = key.split(":", 1)[0]
                comic_id = key.split(":", 1)[1]
                # 合并旧记录：增量/补充下载（如补卷番外）不能覆盖全量下载的数字，
                # 否则书库 images/chapters 会被小值污染（如 1097→49）
                _old = next((x for x in lib if x.get("source") == source
                             and x.get("comic_id") == comic_id), None)
                if _old:
                    images = max(int(_old.get("images") or 0), int(images or 0))
                    total = max(int(_old.get("chapters") or 0), int(total or 0))
                # R32 已下线"最早下载"排序档，first_downloaded_at 无消费方，
                # 且其字符串比较在时钟回拨时会重置首次时间——一并移除
                _now_str = time.strftime("%Y-%m-%d %H:%M:%S")
                lib = [x for x in lib if not (x.get("source") == source
                                              and x.get("comic_id") == comic_id)]
                lib.append({"source": source, "comic_id": comic_id, "title": title,
                            "cover": cover or (_old or {}).get("cover", ""),
                            "chapters": total, "images": images,
                            "status": "done",
                            "downloaded_at": _now_str,
                            "source_name": getattr(ad, "name", source)})
                # 原子替换：复用 atomic_write（含 makedirs+fsync+os.replace），
                # 自建 mkstemp 在目录缺失时会抛 FileNotFoundError 并被吞成日志
                atomic_write(lib_p, lib)
            # B03: 写库成功 → 通知 server 增量重扫该部快照（锁外；钩子异常不影响写库）
            _hook = library_change_hook
            if _hook:
                try:
                    _hook(source, comic_id)
                except Exception:
                    pass
        except Exception as e:
            print(f"[manga-dl] 书库记录失败: {e}", flush=True)

    # ── 路径辅助（由外部注入）──
    _cache_root = None
    _state_dir = None
    _library_file = None

    def _get_cache_root(self):
        return self._cache_root or "data/manga/_cache"

    def _get_downloads_root(self):
        """下载图片目录（永久保留，与临时缓存隔离）。优先级：
        配置的 downloads_root > data/manga/downloads"""
        if getattr(self, "_downloads_root", None):
            return self._downloads_root
        return "data/manga/downloads"

    def _get_state_dir(self):
        return self._state_dir or "data/manga/_state"

    def _get_library_file(self):
        return self._library_file or "data/manga/_library.json"

    def configure(self, cache_root=None, state_dir=None, library_file=None,
                  downloads_root=None):
        self._cache_root = cache_root
        self._state_dir = state_dir
        self._library_file = library_file
        self._downloads_root = downloads_root
        return self


manager = None


def get_manager():
    global manager
    if manager is None:
        manager = DownloadManager()
    return manager
