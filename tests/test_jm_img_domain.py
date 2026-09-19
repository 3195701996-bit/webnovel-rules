# -*- coding: utf-8 -*-
"""禁漫「图片域质量」回归（离线，不触网）

背景（2026-09-14 实测，用户报"在线阅读加载又变慢了"）：同一张 332,958B 的图
在不同图片域上的耗时差 6~10 倍——

    cdn-msp2.jmdanjonproxy.vip   11.0s   ← 引擎当时选中的域
    cdn-msp.jmapiproxy1.cc       13.0s   ← /setting?app_img_shunt=0&express= 返回的域
    cdn-msp3.jmapiproxy3.cc      18.3s
    cdn-msp12.jmdanjonproxy.xyz   1.7s   ← 同一张图
    cdn-msp.jmapinodeudzn.net     1.7s

图片字节完全一致，所以慢在**域**；而旧实现进程启动时取一次 /setting 就再不改，
碰上劣化域整场都慢（单图 20s 超时还会让阅读卡住）。这里把新口径钉死：
候选域、慢/失败即换域、切换后所有取图立即改走新域、切换结果落盘。
"""
import json
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.manga import jm as jmmod  # noqa: E402
from engine.manga.jm import Jm  # noqa: E402

FAST = "https://cdn-msp12.jmdanjonproxy.xyz"
SLOW = "https://cdn-msp2.jmdanjonproxy.vip"
IMG = "/media/photos/1469516/00032.webp"


@pytest.fixture(autouse=True)
def _clear_host_cache():
    jmmod._hosts_cache.update(ts=0.0, hosts=[])
    yield
    jmmod._hosts_cache.update(ts=0.0, hosts=[])


def _adapter(tmp_path, img_domain=SLOW):
    ad = Jm(state_dir=str(tmp_path))
    ad._img_domain = img_domain
    ad._img_refreshed = True          # 默认不联网
    return ad


def test_image_url_rewrites_known_host_to_preferred(tmp_path):
    ad = _adapter(tmp_path, FAST)
    ad._remember_host(SLOW)
    assert ad.image_url(SLOW + IMG) == FAST + IMG, "已知图片域应改写到当前首选域"


def test_image_url_leaves_unknown_hosts_alone(tmp_path):
    ad = _adapter(tmp_path, FAST)
    other = "https://cdn.example.com" + IMG
    assert ad.image_url(other) == other, "未知域不得改写（可能是别的源/直链）"


def test_slow_fetch_switches_when_candidate_measured_faster(tmp_path):
    """慢→换域，但**必须**有实测更快的候选：全都在慢的时候来回换毫无收益。"""
    ad = _adapter(tmp_path, SLOW)
    ad._remember_host(FAST)
    ad._host_cost[FAST.replace("https://", "")] = 1.5     # 备选实测 1.5s
    switched = ad.note_image_result(SLOW + IMG, 11.0, True)
    assert switched is True, "有实测更快的候选时，11s 的取图应触发换域"
    assert ad._img_domain == FAST
    assert ad.image_url(SLOW + IMG) == FAST + IMG, "换域后所有取图立即改走新域"


def test_slow_fetch_keeps_domain_without_faster_candidate(tmp_path):
    """整体劣化时段（备选没实测数据/一样慢）不要来回换域"""
    ad = _adapter(tmp_path, SLOW)
    ad._remember_host(FAST)
    assert ad.note_image_result(SLOW + IMG, 13.0, True) is False
    assert ad._img_domain == SLOW


def test_recently_failed_host_is_not_a_target(tmp_path):
    """刚失败过的域在冷却期内不能被切回去（实测出现过来回跳）"""
    ad = _adapter(tmp_path, SLOW)
    ad._remember_host(FAST)
    ad._host_bad_ts[FAST.replace("https://", "")] = time.time()
    assert ad.note_image_result(SLOW + IMG, 12.0, False) is False
    assert ad._img_domain == SLOW


def test_fast_fetch_keeps_domain(tmp_path):
    ad = _adapter(tmp_path, FAST)
    ad._remember_host(SLOW)
    assert ad.note_image_result(FAST + IMG, 1.7, True) is False
    assert ad._img_domain == FAST


def test_failure_switches_domain(tmp_path):
    ad = _adapter(tmp_path, SLOW)
    ad._remember_host(FAST)
    assert ad.note_image_result(SLOW + IMG, 8.1, False) is True
    assert ad._img_domain == FAST


def test_no_candidate_keeps_domain(tmp_path):
    """只有一个候选时不要瞎切（切了也没得切）"""
    ad = _adapter(tmp_path, SLOW)
    assert ad.note_image_result(SLOW + IMG, 12.0, True) is False
    assert ad._img_domain == SLOW


def test_switch_persists_to_state(tmp_path):
    ad = _adapter(tmp_path, SLOW)
    ad._remember_host(FAST)
    ad.note_image_result(SLOW + IMG, 9.0, False)
    # 新实例（模拟重启）应沿用切换后的域
    again = Jm(state_dir=str(tmp_path))
    assert again._img_domain == FAST, "换域结果要落盘，重启后仍生效"


def test_setting_hosts_are_cached(tmp_path, monkeypatch):
    """候选域探测带 TTL：同一个进程里不许每个请求都去打 /setting"""
    ad = _adapter(tmp_path, SLOW)
    calls = []

    def fake_get(url, timeout=15):
        calls.append(url)
        return json.dumps({"img_host": FAST})

    monkeypatch.setattr(ad, "_get", fake_get)
    first = ad._fetch_setting_host()
    second = ad._fetch_setting_host()
    assert first == FAST == second
    # 最多试两个变体：够拿到候选就停（不值得为候选域等满三倍超时）
    assert 1 <= len(calls) <= 2, "第一次探测的请求数应受控，实际 %d" % len(calls)
    assert ad.candidate_img_hosts()[0] == SLOW.replace("https://", ""), "当前域仍排在最前"


def test_ensure_img_domain_does_not_block_on_setting(tmp_path, monkeypatch):
    """搜索/详情**不得**再同步等 /setting（实测劣化时段要十几秒到几十秒）。

    已有可用域时直接返回；候选留到真正取图时（images()）再探。
    """
    ad = _adapter(tmp_path, FAST)
    ad._img_refreshed = False
    called = []

    def fake_setting():
        called.append(1)
        return SLOW

    monkeypatch.setattr(ad, "_fetch_setting_host", fake_setting)
    ad._ensure_img_domain()
    assert called == [], "已有域时不该去要 /setting（会拖慢搜索与详情）"
    assert ad._img_domain == FAST


def test_ensure_img_domain_initializes_when_missing(tmp_path, monkeypatch):
    """连一个域都没有（首次运行）时才同步取一次 /setting"""
    ad = _adapter(tmp_path, "")
    ad._img_refreshed = False
    monkeypatch.setattr(ad, "_fetch_setting_host", lambda: FAST)
    ad._ensure_img_domain()
    assert ad._img_domain == FAST


def test_timeout_hint_is_bounded(tmp_path):
    """15s：能容纳实测 13s 的慢页，又不至于在卡死的域上白等 20s×3。

    注意**不能**取更小的值：8s 会把"慢但能成"直接变成坏图（实测踩到）。
    """
    ad = _adapter(tmp_path)
    hint = ad.image_timeout_hint()
    assert 10 <= hint <= 20, f"超时建议应在 10~20s，实际 {hint}"
