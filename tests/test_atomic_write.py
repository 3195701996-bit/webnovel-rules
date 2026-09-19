# -*- coding: utf-8 -*-
"""原子写回归（离线）

收藏 / 阅读历史 / 小说进度都经 _write_json 落盘。
原实现 open(p,"w") 截断写：中途崩溃会留下 0 字节或半截 JSON，
_read_json 解析失败回退默认值 = 用户数据整体丢失。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.app_utils import _read_json, _write_json, atomic_write  # noqa: E402


def test_roundtrip(tmp_path):
    p = str(tmp_path / "a.json")
    _write_json(p, {"k": "值"})
    assert _read_json(p, None) == {"k": "值"}


def test_bare_filename_in_cwd(tmp_path, monkeypatch):
    """dirname 为空时 makedirs("") 会抛错——必须回退为 '.'"""
    monkeypatch.chdir(tmp_path)
    _write_json("bare.json", {"b": 2})
    assert _read_json("bare.json", None) == {"b": 2}


def test_creates_missing_parent_dirs(tmp_path):
    p = str(tmp_path / "x" / "y" / "z.json")
    atomic_write(p, {"deep": 1})
    assert _read_json(p, None) == {"deep": 1}


def test_failed_write_keeps_original(tmp_path):
    """序列化失败不得破坏已有文件（截断写会把它清空）"""
    p = str(tmp_path / "keep.json")
    _write_json(p, {"keep": True})
    with pytest.raises(Exception):
        atomic_write(p, {1: object()})
    assert _read_json(p, None) == {"keep": True}


def test_failed_write_leaves_no_tmp(tmp_path):
    p = str(tmp_path / "k.json")
    _write_json(p, {"a": 1})
    with pytest.raises(Exception):
        atomic_write(p, {1: object()})
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_no_tmp_residue_on_success(tmp_path):
    p = str(tmp_path / "ok.json")
    for i in range(5):
        _write_json(p, {"i": i})
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []
    assert _read_json(p, None) == {"i": 4}


def test_overwrite_is_not_truncating(tmp_path):
    """大内容覆盖为小内容后，文件不得残留旧尾部"""
    p = str(tmp_path / "t.json")
    _write_json(p, {"big": "x" * 5000})
    _write_json(p, {"small": 1})
    raw = open(p, encoding="utf-8").read()
    assert json.loads(raw) == {"small": 1}
    assert "x" * 100 not in raw


def test_crash_midway_does_not_corrupt_existing(tmp_path, monkeypatch):
    """核心断言：写入过程中崩溃，已有数据必须完好。

    截断写会先清空目标文件，崩溃后剩 0 字节；
    原子写只动临时文件，目标文件保持上一版本。
    """
    p = str(tmp_path / "fav.json")
    _write_json(p, {"important": "已收藏"})

    real_dump = json.dump

    def _boom(obj, fp, **kw):
        real_dump(obj, fp, **kw)
        fp.flush()
        raise KeyboardInterrupt("模拟写入途中进程被杀")

    # 经 _write_json 触发：它是收藏/历史/进度的真实写入入口，
    # 直接测 atomic_write 无法发现 _write_json 退回截断写的回归
    import engine.app_utils as AU
    monkeypatch.setattr(AU.json, "dump", _boom)
    # KeyboardInterrupt 继承 BaseException，不被 _write_json 的 except Exception 捕获
    with pytest.raises(KeyboardInterrupt):
        AU._write_json(p, {"new": "数据"})
    monkeypatch.undo()

    assert _read_json(p, None) == {"important": "已收藏"}
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_read_json_returns_default_on_corrupt(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    assert _read_json(str(p), {"d": 1}) == {"d": 1}
