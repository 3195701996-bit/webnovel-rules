# -*- coding: utf-8 -*-
"""覆盖修复事务回归（离线、确定性）

覆盖 2026-09-10 结构性优化：
1. 暂存根目录同盘选择：状态目录与目标同盘时用历史路径；跨盘时退到
   <downloads>/.repair_staging（同盘且不在任何 source 目录内）；
2. 事务记录三个状态点（staged/backed_up/switched）在切换过程中落盘，
   成功后被清除；
3. recover_repair_transactions 的收敛规则（"完整"按合法 txn 的 expected
   页数 + 每页图片文件头有效性判定，绝不以任意 1 页冒充完整）：
   - 目标 ≥ expected 张有效图 → 清备份与暂存（完成态）
   - 目标不完整（缺失/页数不足/坏图）+ 备份有有效页 → 备份回滚（保留唯一
     旧完本，绝不清除）
   - 目标/备份都无值 + 暂存 ≥ expected 张 → 完成切换
   - 其余 → 清理暂存
   - 记录 schema 非法/损坏 → 只丢记录；但存在非空暂存/备份时一律保留，
     绝不删除任何不可信路径
4. 跨盘切换显式拒绝（不静默退化为非原子拷贝），原章与暂存保留；
5. app 启动接入 recover（源码断言）+ 修复入口先收敛旧账。

不触网；数据目录由 conftest 隔离。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server.manga_api as MAPI  # noqa: E402


_PAGE_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 52


def _pages(d, n, start=0, ext=".webp"):
    """写入 n 张**头部合法**的 WEBP 页（与 downloader._valid_image_header 对齐）。"""
    os.makedirs(d, exist_ok=True)
    for i in range(start, start + n):
        with open(os.path.join(d, f"{i:04d}{ext}"), "wb") as f:
            f.write(_PAGE_BYTES)


def _txn(staging, target, state, total=3):
    MAPI._repair_txn_write(staging, target, state, total)


# ══════════════════════════════════════════════════════════════
# 1. 同盘暂存选择
# ══════════════════════════════════════════════════════════════

def test_staging_base_same_device_uses_state_dir(tmp_path, monkeypatch):
    target = str(tmp_path / "downloads" / "src" / "cid" / "ch1")
    os.makedirs(target, exist_ok=True)
    monkeypatch.setattr(MAPI, "_same_device", lambda a, b: True)
    base = MAPI._repair_staging_base(target)
    assert base == os.path.join(MAPI.MANGA_STATE_DIR, "_repair_staging")


def test_staging_base_cross_device_uses_downloads_dotdir(tmp_path, monkeypatch):
    target = str(tmp_path / "downloads" / "src" / "cid" / "ch1")
    os.makedirs(target, exist_ok=True)
    monkeypatch.setattr(MAPI, "_same_device", lambda a, b: False)
    base = MAPI._repair_staging_base(target)
    assert base == os.path.join(MAPI.MANGA_DOWNLOADS_DIR, ".repair_staging")
    # 同盘暂存根必须在 downloads 根下、且不落在任何 source 目录内
    assert os.path.dirname(base) == os.path.abspath(MAPI.MANGA_DOWNLOADS_DIR)


def test_staging_base_missing_target_probes_parent(tmp_path, monkeypatch):
    """目标章节目录不存在（全新建）时以父目录（comic 目录）为探测点"""
    target = str(tmp_path / "downloads" / "src" / "cid" / "ch_new")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    seen = {}

    def _probe(a, b):
        seen["a"], seen["b"] = a, b
        return True
    monkeypatch.setattr(MAPI, "_same_device", _probe)
    MAPI._repair_staging_base(target)
    assert seen["b"] == os.path.dirname(target)


# ══════════════════════════════════════════════════════════════
# 2. 事务记录写入/清除
# ══════════════════════════════════════════════════════════════

def test_txn_parent_alias_recovers_same_directory(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    downloads = real / "downloads"
    staging = alias / "staging/src/comic/chapter"
    target = alias / "downloads/src/comic/chapter"
    monkeypatch.setattr(MAPI, "MANGA_DOWNLOADS_DIR", str(downloads))
    _pages(str(staging) + ".old", 3)
    _txn(str(staging), str(target), "backed_up")
    report = MAPI.recover_repair_transactions(str(real / "staging"))
    assert any("回滚" in item["action"] for item in report)
    assert target.is_dir()
    assert MAPI._repair_dir_complete(str(target), 3)
    assert not os.path.exists(MAPI._repair_txn_path(str(staging)))


def test_txn_write_and_clear(tmp_path):
    staging = str(tmp_path / "stg" / "ch1")
    os.makedirs(staging, exist_ok=True)
    target = str(tmp_path / "dl" / "ch1")
    MAPI._repair_txn_write(staging, target, "staged", 7)
    p = MAPI._repair_txn_path(staging)
    data = json.load(open(p, encoding="utf-8"))
    assert data["state"] == "staged" and data["total"] == 7
    assert data["staging"] == staging and data["backup"] == staging + ".old"
    assert data["target"] == target
    MAPI._repair_txn_clear(staging)
    assert not os.path.exists(p)


# ══════════════════════════════════════════════════════════════
# 3. 恢复规则（逐状态）
# ══════════════════════════════════════════════════════════════

def _mk_txn_case(tmp_path, state, *, target_present, backup_pages,
                 staging_pages, total=3):
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    # 恢复要求目标位于书库下载目录内（越界目标一律拒绝）——测试目标须与
    # 生产同构，故置于 MANGA_DOWNLOADS_DIR 下并以 tmp 名保证唯一
    target = os.path.join(MAPI.MANGA_DOWNLOADS_DIR, "srctest",
                          os.path.basename(str(tmp_path)), "ch1")
    if staging_pages:
        _pages(staging, staging_pages)
    if target_present:
        _pages(target, total)
    if backup_pages:
        _pages(staging + ".old", backup_pages)
    _txn(staging, target, state, total)
    return root, staging, target


def test_recover_switched_cleans_backup_and_staging(tmp_path):
    root, staging, target = _mk_txn_case(
        tmp_path, "switched", target_present=True, backup_pages=3,
        staging_pages=0)
    rep = MAPI.recover_repair_transactions(root)
    assert len(rep) == 1
    assert os.path.isdir(target)
    assert not os.path.isdir(staging + ".old"), "已切换 → 清备份"
    assert not os.path.exists(MAPI._repair_txn_path(staging))


def test_recover_backed_up_rolls_back_when_target_missing(tmp_path):
    """崩溃在两次 rename 之间：目标缺失、备份是唯一可读版本 → 回滚"""
    root, staging, target = _mk_txn_case(
        tmp_path, "backed_up", target_present=False, backup_pages=3,
        staging_pages=3)
    rep = MAPI.recover_repair_transactions(root)
    assert len(rep) == 1
    assert os.path.isdir(target) and MAPI._repair_dir_pages(target) == 3
    assert not os.path.isdir(staging + ".old")
    assert not os.path.exists(MAPI._repair_txn_path(staging))


def test_recover_backed_up_target_present_treated_as_switched(tmp_path):
    root, staging, target = _mk_txn_case(
        tmp_path, "backed_up", target_present=True, backup_pages=3,
        staging_pages=0)
    MAPI.recover_repair_transactions(root)
    assert MAPI._repair_dir_pages(target) == 3
    assert not os.path.isdir(staging + ".old")


def test_recover_staged_discards_when_target_present(tmp_path):
    """暂存完整但旧版本还在位（崩溃在切换前）→ 丢弃暂存，原章可用"""
    root, staging, target = _mk_txn_case(
        tmp_path, "staged", target_present=True, backup_pages=0,
        staging_pages=3)
    MAPI.recover_repair_transactions(root)
    assert os.path.isdir(target) and MAPI._repair_dir_pages(target) == 3
    assert not os.path.isdir(staging)
    assert not os.path.exists(MAPI._repair_txn_path(staging))


def test_recover_staged_finalizes_when_target_vanished(tmp_path):
    """目标与备份都不在、暂存完整 → 完成切换（保住已下载内容）"""
    root, staging, target = _mk_txn_case(
        tmp_path, "staged", target_present=False, backup_pages=0,
        staging_pages=3)
    MAPI.recover_repair_transactions(root)
    assert os.path.isdir(target) and MAPI._repair_dir_pages(target) == 3
    assert not os.path.isdir(staging)


def test_recover_staged_incomplete_cleans_staging(tmp_path):
    """目标缺失、无备份、暂存不完整 → 清暂存（无法恢复旧版本）"""
    root, staging, target = _mk_txn_case(
        tmp_path, "staged", target_present=False, backup_pages=0,
        staging_pages=0)
    MAPI.recover_repair_transactions(root)
    assert not os.path.isdir(target)
    assert not os.path.isdir(staging)


def test_recover_corrupt_txn_discarded(tmp_path):
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    os.makedirs(staging, exist_ok=True)
    p = MAPI._repair_txn_path(staging)
    with open(p, "w", encoding="utf-8") as f:
        f.write("{ not json")
    rep = MAPI.recover_repair_transactions(root)
    assert rep and "丢弃" in rep[0]["action"], "无价值数据 → 仅丢弃记录"
    assert not os.path.exists(p), "损坏记录应被丢弃"


def test_recover_missing_root_is_noop(tmp_path):
    assert MAPI.recover_repair_transactions(str(tmp_path / "nope")) == []


# ══════════════════════════════════════════════════════════════
# 4. 启动接入 + 修复入口先收敛旧账
# ══════════════════════════════════════════════════════════════

def test_app_startup_calls_recovery():
    """启动恢复接入：模块级（WSGI/导入式启动）+ 显式 initialize（主入口）。

    阶段 B 起，后台/初始化副作用收敛到 server.runtime 的显式钩子：main() 不再
    直接调用恢复函数，而是 `_RUNTIME.initialize()`，其钩子里有 repair_transactions。
    因此这里断言"行为契约"而不是 main() 的字面源码：
      ① 模块级仍会调用（WSGI 只加载模块的部署路径）；
      ② 运行时的 initialize 钩子确实包含该恢复函数（main()/移动端都会走它）。"""
    import inspect
    import app as APP
    src = inspect.getsource(APP)
    assert "recover_repair_transactions" in src, \
        "启动必须执行覆盖修复事务恢复"
    assert "_boot_recover_repair_transactions()" in src, \
        "启动恢复须在模块级或 main() 中实际调用"
    # ② 显式启动路径（main() 与移动端共用）：main() 走 _RUNTIME.start()，
    #    其内部先执行 initialize() 钩子（含本恢复）；旧的 main() 直接调用已
    #    在阶段 B 收敛到控制器。
    _main_src = inspect.getsource(APP.main)
    assert ("_RUNTIME.start(" in _main_src) or ("_RUNTIME.initialize(" in _main_src), \
        "main() 必须经过 runtime 的显式启动/初始化触发恢复钩子"
    hook_fns = [fn for item in APP._RUNTIME._hooks
                for fn in [item[1] if isinstance(item, tuple) else item]]
    assert APP._boot_recover_repair_transactions in hook_fns, \
        "initialize 钩子必须包含覆盖修复事务恢复"


def test_repair_endpoint_recovers_old_transaction_first(tmp_path, monkeypatch):
    """修复入口进入覆盖模式时先收敛旧事务：被打断的章节必须先回滚"""
    import app as APP
    APP.app.config["TESTING"] = True
    client = APP.app.test_client()
    from engine.config import MANGA_DOWNLOADS_DIR

    source, comic_id, chapter_id = "copymanga_web", "cidrec", "chrec"
    target = os.path.join(MANGA_DOWNLOADS_DIR, source, comic_id, chapter_id)
    # 造"崩溃在两次 rename 之间"的现场：备份在暂存根、目标缺失
    base = MAPI._repair_staging_base(target)
    staging = os.path.join(base, source, comic_id, chapter_id)
    _pages(staging + ".old", 3)
    _txn(staging, target, "backed_up", 3)

    # 打桩取图：清单为空 → 端点在建暂存前就应已收敛旧事务（回滚）
    class _FakeWeb:
        def images(self, cid, chid):
            return []
    monkeypatch.setattr("engine.manga.copymanga_web.CopyMangaWeb",
                        lambda *a, **k: _FakeWeb())
    r = client.post(f"/api/manga/{source}/{comic_id}/chapter/{chapter_id}/repair",
                    json={"overwrite": True})
    assert r.status_code == 500          # 清单为空 → 保留原章
    assert os.path.isdir(target), "旧事务必须先回滚恢复原章节"
    assert MAPI._repair_dir_pages(target) == 3
    assert not os.path.exists(MAPI._repair_txn_path(staging))


# ══════════════════════════════════════════════════════════════
# 5. 路径越界 / 软链逃逸：绝不删除受控根之外的数据
# ══════════════════════════════════════════════════════════════

def test_recover_out_of_bounds_staging_not_deleted(tmp_path):
    """记录内 staging 指向扫描根之外的目录（记录指针被改写/损坏）→
    记录 schema 不符 → 只丢记录，绝不删除任何指向路径"""
    root = str(tmp_path / "stgroot")
    os.makedirs(root, exist_ok=True)
    victim = tmp_path / "victim"
    _pages(str(victim), 3)
    txn_p = os.path.join(root, "evil.txn.json")
    with open(txn_p, "w", encoding="utf-8") as f:
        json.dump({"state": "switched", "staging": str(victim),
                   "backup": str(victim) + ".old",
                   "target": str(tmp_path / "t")}, f)
    rep = MAPI.recover_repair_transactions(root)
    assert os.path.isdir(str(victim)), "越界暂存目录不得被删除"
    assert MAPI._repair_dir_pages(str(victim)) == 3
    assert not os.path.exists(txn_p), "非法记录应被丢弃"
    assert rep, "非法记录须有处置记录"


def test_recover_symlink_staging_escape_refused(tmp_path):
    """暂存是符号链接指向外部目录 → 软链解析后越界，拒绝且不删软链/目标"""
    root = str(tmp_path / "stgroot")
    os.makedirs(root, exist_ok=True)
    victim = tmp_path / "realvictim"
    _pages(str(victim), 3)
    link = os.path.join(root, "link_ch")
    os.symlink(str(victim), link)
    txn_p = os.path.join(root, "link_ch.txn.json")
    # schema 合法（staging 与文件名一致、total 正、state 合法）→ 才会走到
    # 路径越界判定；软链解析后落在扫描根之外，必须被 _repair_within 拒绝
    with open(txn_p, "w", encoding="utf-8") as f:
        json.dump({"state": "switched", "staging": link,
                   "backup": link + ".old", "total": 3,
                   "target": os.path.join(MAPI.MANGA_DOWNLOADS_DIR, "s",
                                          "c", "ch1")}, f)
    rep = MAPI.recover_repair_transactions(root)
    assert os.path.isdir(str(victim))
    assert MAPI._repair_dir_pages(str(victim)) == 3, "软链逃逸不得删除真实目录"
    assert os.path.islink(link), "符号链接本身也不得被删"
    assert rep and "越界" in rep[0]["action"]


def test_recover_multilevel_symlink_escape_refused(tmp_path):
    """多级软链：root/a -> root/b -> victim（外部）→ 解析后越界，拒绝且不删"""
    root = str(tmp_path / "stgroot")
    os.makedirs(root, exist_ok=True)
    victim = tmp_path / "deepvictim"
    _pages(str(victim), 3)
    os.symlink(str(victim), os.path.join(root, "b"))   # 一层
    os.symlink(os.path.join(root, "b"), os.path.join(root, "a"))  # 二层
    staging = os.path.join(root, "a")
    txn_p = os.path.join(root, "a.txn.json")
    with open(txn_p, "w", encoding="utf-8") as f:
        json.dump({"state": "switched", "staging": staging,
                   "backup": staging + ".old", "total": 3,
                   "target": os.path.join(MAPI.MANGA_DOWNLOADS_DIR, "s",
                                          "c", "ch1")}, f)
    rep = MAPI.recover_repair_transactions(root)
    assert MAPI._repair_dir_pages(str(victim)) == 3, "多级软链不得删除真实目录"
    assert os.path.islink(staging) and os.path.islink(os.path.join(root, "b"))
    assert rep and "越界" in rep[0]["action"]


def test_recover_out_of_bounds_target_refused(tmp_path):
    """目标不在书库下载目录内 → 拒绝改名/清理，暂存原样保留"""
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    _pages(staging, 3)
    outside = tmp_path / "outside_target"
    _pages(str(outside), 3)
    _txn(staging, str(outside), "switched", 3)
    rep = MAPI.recover_repair_transactions(root)
    assert os.path.isdir(str(outside))
    assert MAPI._repair_dir_pages(str(outside)) == 3
    assert rep and "越界" in rep[0]["action"]
    assert os.path.isdir(staging), "拒绝时不得删除暂存"


# ══════════════════════════════════════════════════════════════
# 6. 守护旧数据：不完整的"在位目标"与损坏记录不得吞掉唯一完本
# ══════════════════════════════════════════════════════════════

def test_recover_empty_target_rolls_back_backup(tmp_path):
    """目标存在但为 0 页（半成品）：不得当"已完成"而删掉唯一备份，须回滚"""
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    target = os.path.join(MAPI.MANGA_DOWNLOADS_DIR, "srcempty",
                          os.path.basename(str(tmp_path)), "ch1")
    os.makedirs(target, exist_ok=True)      # 空目录：0 页
    _pages(staging + ".old", 3)             # 备份 = 唯一完本
    _pages(staging, 3)
    _txn(staging, target, "backed_up", 3)
    rep = MAPI.recover_repair_transactions(root)
    assert MAPI._repair_dir_pages(target) == 3, "空目标必须用备份回滚"
    assert not os.path.isdir(staging + ".old")
    assert rep and "回滚" in rep[0]["action"]


def test_recover_corrupt_record_preserves_unique_backup(tmp_path):
    """损坏记录 + 非空备份（唯一完本）→ 保留，绝不删除"""
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    _pages(staging + ".old", 3)
    p = MAPI._repair_txn_path(staging)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("{ not json")
    rep = MAPI.recover_repair_transactions(root)
    assert os.path.isdir(staging + ".old"), "损坏记录不得导致唯一备份被删"
    assert MAPI._repair_dir_pages(staging + ".old") == 3
    assert rep and "保留" in rep[0]["action"]


def test_recover_corrupt_record_empty_staging_discarded(tmp_path):
    """损坏记录 + 无有价值数据（空暂存、无备份）→ 丢弃记录不残留噪声"""
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    os.makedirs(staging, exist_ok=True)
    p = MAPI._repair_txn_path(staging)
    with open(p, "w", encoding="utf-8") as f:
        f.write("{ not json")
    rep = MAPI.recover_repair_transactions(root)
    assert rep and "丢弃" in rep[0]["action"]
    assert not os.path.exists(p)


# ══════════════════════════════════════════════════════════════
# 6b. 部分目标（1/3 页）与坏图目标：不得冒充完整、不得吞掉唯一旧完本
# ══════════════════════════════════════════════════════════════

def test_recover_partial_target_backed_up_rolls_back_backup(tmp_path):
    """target 只有 1/3 页且备份完整 → 目标不合格，保守回滚旧完本"""
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    target = os.path.join(MAPI.MANGA_DOWNLOADS_DIR, "srcpart",
                          os.path.basename(str(tmp_path)), "ch1")
    _pages(target, 1)                 # 半成品目标：仅 1/3
    _pages(staging + ".old", 3)       # 唯一旧完本
    _pages(staging, 3)
    _txn(staging, target, "backed_up", 3)
    rep = MAPI.recover_repair_transactions(root)
    assert MAPI._repair_dir_pages(target) == 3, "1/3 页目标必须以备份回滚"
    assert not os.path.isdir(staging + ".old")
    assert rep and "回滚" in rep[0]["action"]


def test_recover_partial_target_staged_rolls_back_backup(tmp_path):
    """state=staged 且 target 页数不足、备份完整 → 同样保守回滚"""
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    target = os.path.join(MAPI.MANGA_DOWNLOADS_DIR, "srcstag",
                          os.path.basename(str(tmp_path)), "ch1")
    _pages(target, 1)
    _pages(staging + ".old", 3)
    _pages(staging, 3)
    _txn(staging, target, "staged", 3)
    MAPI.recover_repair_transactions(root)
    assert MAPI._repair_dir_pages(target) == 3
    assert not os.path.isdir(staging + ".old")


def test_recover_partial_target_no_backup_finalizes_staging(tmp_path):
    """target 页数不足但无备份、暂存完整 → 完成切换（用新版本覆盖半成品）"""
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    target = os.path.join(MAPI.MANGA_DOWNLOADS_DIR, "srcstag2",
                          os.path.basename(str(tmp_path)), "ch1")
    _pages(target, 1)
    _pages(staging, 3)
    _txn(staging, target, "staged", 3)
    MAPI.recover_repair_transactions(root)
    assert MAPI._repair_dir_pages(target) == 3
    assert not os.path.isdir(staging)


def test_recover_bad_image_target_not_treated_complete(tmp_path):
    """target 页数达标但图全部坏（0 字节）→ 不算完整，回滚唯一完本"""
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    target = os.path.join(MAPI.MANGA_DOWNLOADS_DIR, "srcbad",
                          os.path.basename(str(tmp_path)), "ch1")
    os.makedirs(target, exist_ok=True)
    for i in range(3):                # 3 个 0 字节"图"：页数达标、内容无效
        with open(os.path.join(target, f"{i:04d}.webp"), "wb") as f:
            f.write(b"")
    _pages(staging + ".old", 3)
    _txn(staging, target, "backed_up", 3)
    rep = MAPI.recover_repair_transactions(root)
    assert rep and "回滚" in rep[0]["action"]
    assert MAPI._repair_dir_pages(target) == 3
    assert not os.path.isdir(staging + ".old")


# ══════════════════════════════════════════════════════════════
# 6c. 目标等于受控根本身：不得把书库根 / source 根整体删改
# ══════════════════════════════════════════════════════════════

def test_recover_target_equals_downloads_root_refused(tmp_path):
    """target == 书库下载根 → 拒绝，绝不改名/删除整个书库根"""
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    _pages(staging, 3)
    _txn(staging, MAPI.MANGA_DOWNLOADS_DIR, "backed_up", 3)
    rep = MAPI.recover_repair_transactions(root)
    assert os.path.isdir(MAPI.MANGA_DOWNLOADS_DIR), "书库根绝不能被改名/删除"
    assert os.path.isdir(staging), "拒绝时不得删除暂存"
    assert rep and "越界" in rep[0]["action"]


def test_recover_target_equals_source_root_refused(tmp_path):
    """target == 某个 source 根 → 拒绝，绝不把 source 根整体当章节改名"""
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    _pages(staging, 3)
    src_root = os.path.join(MAPI.MANGA_DOWNLOADS_DIR, "srconly")
    os.makedirs(src_root, exist_ok=True)
    _txn(staging, src_root, "backed_up", 3)
    rep = MAPI.recover_repair_transactions(root)
    assert os.path.isdir(src_root), "source 根不能被当目标改名/删除"
    assert os.path.isdir(staging)
    assert rep and "越界" in rep[0]["action"]


# ══════════════════════════════════════════════════════════════
# 6d. 事务记录 schema 校验：字段不符只丢记录，绝不触碰任何路径
# ══════════════════════════════════════════════════════════════

def _write_txn(txn_p, payload):
    os.makedirs(os.path.dirname(txn_p), exist_ok=True)
    with open(txn_p, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def test_recover_schema_staging_mismatch_discarded(tmp_path):
    """记录 staging 与文件名不符（指针被改写）→ 丢记录、不碰其指向的路径"""
    root = str(tmp_path / "stgroot")
    victim = tmp_path / "theviction"
    _pages(str(victim), 3)
    txn_p = os.path.join(root, "src", "cid", "ch1.txn.json")
    _write_txn(txn_p, {"state": "switched", "staging": str(victim),
                       "target": os.path.join(MAPI.MANGA_DOWNLOADS_DIR, "s",
                                              "c", "x"), "total": 3})
    rep = MAPI.recover_repair_transactions(root)
    assert os.path.isdir(str(victim))
    assert MAPI._repair_dir_pages(str(victim)) == 3, "指针指向的目录不得被删"
    assert not os.path.exists(txn_p)
    assert rep


def test_recover_schema_bad_state_discarded(tmp_path):
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    os.makedirs(staging, exist_ok=True)
    txn_p = MAPI._repair_txn_path(staging)
    _write_txn(txn_p, {"state": "bogus", "staging": staging,
                       "target": os.path.join(MAPI.MANGA_DOWNLOADS_DIR, "s",
                                              "c", "x"), "total": 3})
    rep = MAPI.recover_repair_transactions(root)
    assert not os.path.exists(txn_p)
    assert rep


def test_recover_schema_bad_total_discarded(tmp_path):
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    os.makedirs(staging, exist_ok=True)
    txn_p = MAPI._repair_txn_path(staging)
    _write_txn(txn_p, {"state": "staged", "staging": staging,
                       "target": os.path.join(MAPI.MANGA_DOWNLOADS_DIR, "s",
                                              "c", "x"), "total": 0})
    rep = MAPI.recover_repair_transactions(root)
    assert not os.path.exists(txn_p)
    assert rep


def test_recover_schema_missing_target_discarded(tmp_path):
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    os.makedirs(staging, exist_ok=True)
    txn_p = MAPI._repair_txn_path(staging)
    _write_txn(txn_p, {"state": "staged", "staging": staging, "total": 3})
    rep = MAPI.recover_repair_transactions(root)
    assert not os.path.exists(txn_p)
    assert rep


# ══════════════════════════════════════════════════════════════
# 6e. 并发：写入者持 flock 期间恢复必须阻塞（确定性，非 sleep 竞速）
# ══════════════════════════════════════════════════════════════

def test_concurrent_recovery_blocked_by_writer_lock(tmp_path):
    """同一章：写入者持 <staging>.txn.lock 的 flock 期间，恢复者必须阻塞，
    绝不能跑到改名/删目录（同进程不同线程各自 fd 亦由 flock 互斥）"""
    import threading
    root, staging, target = _mk_txn_case(
        tmp_path, "backed_up", target_present=False, backup_pages=3,
        staging_pages=0)
    holding = threading.Event()
    release = threading.Event()

    def _writer():
        with MAPI._repair_txn_lock(staging):
            holding.set()
            release.wait(5)

    def _recover():
        MAPI.recover_repair_transactions(only=staging)

    w = threading.Thread(target=_writer)
    w.start()
    assert holding.wait(5), "写入者未能取得事务锁"
    r = threading.Thread(target=_recover)
    r.start()
    r.join(timeout=0.6)
    assert r.is_alive(), "恢复者必须被写入者的 per-chapter flock 阻塞"
    assert not os.path.isdir(target), "锁未释放前不得发生回滚改名"
    assert os.path.isdir(staging + ".old"), "锁未释放前不得删备份"
    release.set()
    w.join(5)
    r.join(10)
    assert not r.is_alive()
    assert os.path.isdir(target) and MAPI._repair_dir_pages(target) == 3
    assert not os.path.isdir(staging + ".old")
    assert not os.path.exists(MAPI._repair_txn_path(staging))


# ══════════════════════════════════════════════════════════════
# 6f. 页集合完整性：按 idx 去重，必须覆盖 0..expected-1（勿被重复格式骗）
# ══════════════════════════════════════════════════════════════

def test_repair_valid_pages_requires_complete_deduped_set(tmp_path):
    d = str(tmp_path / "d")
    _pages(d, 3)                                   # 0000..0002 合法
    assert MAPI._repair_valid_pages(d, 3)
    assert not MAPI._repair_valid_pages(d, 4)      # 缺 0003
    _pages(d, 1, ext=".jpg")                       # 再加 0000.jpg（同 idx 重复格式）
    assert not MAPI._repair_valid_pages(d, 4)
    assert MAPI._repair_valid_pages(d, 3), "重复格式不得改变已完整集合的判定"


def test_repair_valid_pages_dup_format_not_counted_as_pages(tmp_path):
    """仅 0000.webp + 0000.jpg（两个文件、同一 idx）→ 只算 1 页，expected=2 判否"""
    d = str(tmp_path / "d")
    _pages(d, 1, ext=".webp")
    _pages(d, 1, ext=".jpg")
    assert MAPI._repair_valid_pages(d, 1)
    assert not MAPI._repair_valid_pages(d, 2), "重复格式不得冒充 2 页"


def test_recover_dup_format_target_not_complete_rolls_back(tmp_path):
    """目标用重复扩展名把 1 页伪装成"2 页"，不得因此判完整而清掉旧完本"""
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    target = os.path.join(MAPI.MANGA_DOWNLOADS_DIR, "srcdup",
                          os.path.basename(str(tmp_path)), "ch1")
    _pages(target, 1, ext=".webp")
    _pages(target, 1, ext=".jpg")      # 同 idx 重复 → 实际仅 1 页（expected=2）
    _pages(staging + ".old", 2)        # 旧完本（0..1）
    _txn(staging, target, "switched", 2)
    rep = MAPI.recover_repair_transactions(root)
    assert rep and "回滚" in rep[0]["action"]
    assert MAPI._repair_valid_pages(target, 2), "必须回滚出完整旧完本"
    assert not os.path.isdir(staging + ".old")


# ══════════════════════════════════════════════════════════════
# 6g. 取锁后在锁内重读 + 写入者整个周期持锁（确定性）
# ══════════════════════════════════════════════════════════════

def test_repair_apply_locked_noop_when_record_vanished(tmp_path):
    """等锁期间记录已被清除 → 锁内重读发现不存在，绝不动作、不报错"""
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    _pages(staging, 3)
    txn_p = MAPI._repair_txn_path(staging)   # 不写记录 = 已被清除
    rep = []
    MAPI._repair_apply_locked(txn_p, root, rep)   # 不抛
    assert rep == []
    assert os.path.isdir(staging), "无事务时不得删暂存"


def test_repair_apply_uses_current_on_disk_record(tmp_path):
    """基于锁内最新记录动作：磁盘为 staged + 暂存完整 → 完成切换"""
    root = str(tmp_path / "stgroot")
    staging = os.path.join(root, "src", "cid", "ch1")
    target = os.path.join(MAPI.MANGA_DOWNLOADS_DIR, "srccur",
                          os.path.basename(str(tmp_path)), "ch1")
    _pages(staging, 3)                       # 暂存完整、无备份、目标缺失
    _txn(staging, target, "staged", 3)
    rep = MAPI.recover_repair_transactions(root)
    assert rep and "完成切换" in rep[0]["action"]
    assert MAPI._repair_valid_pages(target, 3)
    assert not os.path.isdir(staging)


def test_overwrite_holds_txn_lock_across_download(client, monkeypatch):
    """写入者的**下载**阶段必须已持有本章事务锁（锁覆盖整个准备+下载+切换）：
    下载中由另一线程尝试获取同章本地锁必须失败"""
    import threading
    d = _seed_chapter("ov_lock_cycle", "ch1", pages=3, content=_WEBP)
    urls = [f"https://img.example.com/lk_{i}.webp" for i in range(4)]
    _patch_web(monkeypatch, _FakeWeb(imgs=urls))
    _patch_fetch(monkeypatch)
    staging = os.path.join(MAPI._repair_staging_base(d),
                           _SRC, "ov_lock_cycle", "ch1")
    real_fetch = MAPI._repair_fetch_pages
    probe = {}

    def _spy(imgs, target_dir, indexes):
        res = {}

        def _try():
            got = MAPI._repair_local_lock(staging).acquire(blocking=False)
            res["locked"] = not got
            if got:
                MAPI._repair_local_lock(staging).release()
        t = threading.Thread(target=_try)
        t.start()
        t.join(3)
        probe["locked"] = res.get("locked")
        return real_fetch(imgs, target_dir, indexes)

    monkeypatch.setattr(MAPI, "_repair_fetch_pages", _spy)
    r = _repair(client, "ov_lock_cycle", "ch1")
    assert r.status_code == 200 and r.get_json().get("ok")
    assert probe.get("locked") is True, "下载期间必须已持有本章事务锁"


# ══════════════════════════════════════════════════════════════
# 7. 写记录失败的可恢复性（故障注入，走真实修复端点）
# ══════════════════════════════════════════════════════════════

_WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x11" * 2000
_WEBP_NEW = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x22" * 2000
_SRC = "copymanga"


@pytest.fixture()
def client():
    import app as APP
    APP.app.config["TESTING"] = True
    return APP.app.test_client()


def _seed_chapter(comic, chapter, pages=3, content=_WEBP):
    from engine.config import MANGA_DOWNLOADS_DIR
    d = os.path.join(MANGA_DOWNLOADS_DIR, _SRC, comic, chapter)
    os.makedirs(d, exist_ok=True)
    for i in range(pages):
        with open(os.path.join(d, f"{i:04d}.webp"), "wb") as f:
            f.write(content)
    return d


class _FakeWeb:
    def __init__(self, imgs=None, exc=None):
        self._imgs = imgs
        self._exc = exc

    def images(self, comic_id, chapter_id):
        if self._exc is not None:
            raise self._exc
        return list(self._imgs or [])


def _patch_web(monkeypatch, fake):
    import engine.manga.manager as mgr
    monkeypatch.setattr(mgr, "get_adapter", lambda key, **kw: fake)


def _patch_fetch(monkeypatch):
    import engine.manga.downloader as dl

    class _Resp:
        status_code = 200
        content = _WEBP_NEW

    monkeypatch.setattr(dl, "fetch_image_checked", lambda url, **kw: _Resp())


def _repair(client, comic, chapter):
    return client.post(
        f"/api/manga/{_SRC}/{comic}/chapter/{chapter}/repair",
        json={"overwrite": True})


def test_backed_up_record_write_failure_restores_original(client, monkeypatch):
    """事务点②记录写失败：立即回滚，原章完整（不留悬空备份）"""
    d = _seed_chapter("ov_bu_writefail", "ch1", pages=3, content=_WEBP)
    urls = [f"https://img.example.com/x_{i}.webp" for i in range(4)]
    _patch_web(monkeypatch, _FakeWeb(imgs=urls))
    _patch_fetch(monkeypatch)
    real = MAPI._repair_txn_write

    def _flaky(staging, target, state, total=0, extra=None):
        if state == "backed_up":
            raise OSError("disk full")
        return real(staging, target, state, total, extra)
    monkeypatch.setattr(MAPI, "_repair_txn_write", _flaky)

    r = _repair(client, "ov_bu_writefail", "ch1")
    assert r.status_code != 200 or not r.get_json().get("ok")
    assert sorted(os.listdir(d)) == ["0000.webp", "0001.webp", "0002.webp"]
    for i in range(3):
        with open(os.path.join(d, f"{i:04d}.webp"), "rb") as f:
            assert f.read() == _WEBP, "原章内容必须原样保留"
    staging = os.path.join(MAPI._repair_staging_base(d),
                           _SRC, "ov_bu_writefail", "ch1")
    assert not os.path.isdir(staging + ".old")
    assert not os.path.exists(MAPI._repair_txn_path(staging))


def test_switched_record_write_failure_keeps_recoverable_state(
        client, monkeypatch):
    """事务点③记录写失败：目标已是新版本（成功），保留备份与记录 → 下次
    恢复按"目标在位"清理，可恢复且不丢数据"""
    d = _seed_chapter("ov_sw_writefail", "ch1", pages=3, content=_WEBP)
    urls = [f"https://img.example.com/new_{i}.webp" for i in range(4)]
    _patch_web(monkeypatch, _FakeWeb(imgs=urls))
    _patch_fetch(monkeypatch)
    real = MAPI._repair_txn_write

    def _flaky(staging, target, state, total=0, extra=None):
        if state == "switched":
            raise OSError("disk full")
        return real(staging, target, state, total, extra)
    monkeypatch.setattr(MAPI, "_repair_txn_write", _flaky)

    r = _repair(client, "ov_sw_writefail", "ch1")
    assert r.status_code == 200 and r.get_json().get("ok")
    assert sorted(os.listdir(d)) == ["0000.webp", "0001.webp",
                                     "0002.webp", "0003.webp"]
    for i in range(4):
        with open(os.path.join(d, f"{i:04d}.webp"), "rb") as f:
            assert f.read() == _WEBP_NEW
    staging = os.path.join(MAPI._repair_staging_base(d),
                           _SRC, "ov_sw_writefail", "ch1")
    assert os.path.isdir(staging + ".old"), "记录未落盘时唯一备份必须保留"
    assert os.path.exists(MAPI._repair_txn_path(staging)), \
        "记录未落盘时不得清除记录（保持可恢复状态）"
    rep = MAPI.recover_repair_transactions(only=staging)
    assert rep and "目标完整" in rep[0]["action"]
    assert not os.path.isdir(staging + ".old")
    assert not os.path.exists(MAPI._repair_txn_path(staging))
    assert sorted(os.listdir(d)) == ["0000.webp", "0001.webp",
                                     "0002.webp", "0003.webp"]


# ══════════════════════════════════════════════════════════════
# 8. 本地子进程崩溃验证（在两次 rename 之间进程死亡）
# ══════════════════════════════════════════════════════════════

def test_local_subprocess_crash_between_renames_recovers(tmp_path,
                                                         monkeypatch):
    """轻量子进程崩溃注入：子进程写 backup+记录后立即 os._exit（模拟
    "旧目录已改名、暂存尚未改名"时死亡），父进程按记录回滚 → 原章可读。

    完全隔离：子进程与父进程同指 tmp_path 下的数据根，绝不触碰任何真实
    数据；清理仅由 pytest 的 tmp_path fixture 完成，测试本身不删"原文件"。"""
    import subprocess
    data_dir = str(tmp_path / "cdata")            # 隔离数据根（DATA_DIR）
    dl = os.path.join(data_dir, "manga", "downloads")
    st = os.path.join(data_dir, "manga", "_state")
    os.makedirs(dl, exist_ok=True)
    os.makedirs(st, exist_ok=True)
    # 父进程恢复逻辑指向同一隔离数据根（engine.config 布局：DATA/manga/...）
    monkeypatch.setattr(MAPI, "MANGA_DOWNLOADS_DIR", dl)
    monkeypatch.setattr(MAPI, "MANGA_STATE_DIR", st)
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = os.path.join(st, "_repair_staging")
    src, cid, ch = "copyx", "cidcrash", "chcrash"
    staging = os.path.join(base, src, cid, ch)
    target = os.path.join(dl, src, cid, ch)
    code = (
        "import os,sys;"
        "sys.path.insert(0, {proj!r});"
        "import server.manga_api as M;"
        "s = {stg!r};"
        "os.makedirs(s + '.old', exist_ok=True);"
        "[(lambda p: (open(p, 'wb').write("
        "b'RIFF' + bytes(4) + b'WEBP' + bytes(52))))"
        "(os.path.join(s + '.old', '%04d.webp' % i)) for i in range(3)];"
        "M._repair_txn_write(s, {tgt!r}, 'backed_up', 3);"
        "os._exit(7)"
    ).format(proj=proj, stg=staging, tgt=target)
    env = dict(os.environ)
    env.update({"WR_DATA_DIR": data_dir, "WR_TEST": "1",
                "WR_DISABLE_BACKGROUND": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": proj})
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd=proj,
                       capture_output=True, timeout=180)
    assert r.returncode == 7, r.stderr.decode("utf-8", "replace")[-1500:]
    # 崩溃现场：目标缺失、备份是唯一完本
    assert not os.path.isdir(target)
    assert os.path.isdir(staging + ".old")
    rep = MAPI.recover_repair_transactions(base)
    assert any(x.get("target") == target for x in rep)
    assert MAPI._repair_dir_pages(target) == 3, "崩溃后原章必须被恢复"
    assert not os.path.isdir(staging + ".old")
    assert not os.path.exists(MAPI._repair_txn_path(staging))
