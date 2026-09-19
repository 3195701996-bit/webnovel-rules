# -*- coding: utf-8 -*-
"""内置源种子策略的离线契约（0.74.5 加的守卫）。

## 为什么需要这条测试

`android/mobile-seed-policy.json` 决定**手机首装时哪些源默认启用**。
它的键是书源 **uid**（不是域名！）——0.74.5 我第一次改它时写成了域名，
结果策略不生效：本该停用的 6 个死域源仍然被搜到（实测"22 个源仍被跑到"）。
这类错误不会报错，只会**静默地什么都不做**，所以必须有离线断言。

另外守住两条纪律：
- 停用的源必须在 `sources/` 里真实存在（防止写错键/源被删后留下僵尸条目）；
- 必须写明**理由**（指南：停用要能向用户解释原因），且不能把源全停掉。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY = os.path.join(ROOT, "android", "mobile-seed-policy.json")


def _policy():
    return json.load(open(POLICY, encoding="utf-8"))


def _uids():
    """sources/ 里全部 uid（与 Gradle 生成清单时的口径一致）"""
    out = set()
    d = os.path.join(ROOT, "sources")
    for fn in os.listdir(d):
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        try:
            j = json.load(open(os.path.join(d, fn), encoding="utf-8"))
        except Exception:
            continue
        out.add((j.get("uid") or fn[:-5]).strip())
    return out


def test_disabled_uids_exist_in_sources():
    """键必须是**真实存在的 uid**：写错键不会报错，只会静默失效"""
    have = _uids()
    miss = [k for k in (_policy().get("disable") or {}) if k not in have]
    assert not miss, (
        "种子策略里这些键在 sources/ 里找不到对应 uid（策略按 uid 匹配，不是域名）："
        + ", ".join(miss))


def test_every_disable_has_reason():
    """停用必须写明理由：用户看到"已停用"时要能知道为什么"""
    bad = [k for k, v in (_policy().get("disable") or {}).items()
           if not (v or "").strip()]
    assert not bad, "这些源停用却没写理由：%s" % bad


def test_not_everything_is_disabled():
    """不能把源全停掉（那不是修复，是把功能关掉）"""
    pol = _policy()
    dis = pol.get("disable") or {}
    total = len(_uids())
    assert total > 0
    assert len(dis) < total, "停用数 %d 不得 >= 源总数 %d" % (len(dis), total)
    assert len(dis) <= total // 2, (
        "停用比例过高（%d/%d）：停用应当是例外，不是默认" % (len(dis), total))


def test_gradle_revision_includes_policy():
    """revision 必须把**策略**也算进去，否则"只改策略"到不了已安装的设备。

    2026-09-18 实测：把 8 个死域源加入停用表后 revision 仍是 `4eb8fe063497`
    （它只由源文件内容算），而设备端 `applySeed` 在 schema/revision/logic 全没变时
    直接 return 0 —— 于是**策略静默失效**，只有全新安装才生效。
    Gradle 注释里写的是"策略/规则变化靠 revision 触发重新合并"，实现当时并没有做到。
    """
    g = open(os.path.join(ROOT, "android", "app", "build.gradle"), encoding="utf-8").read()
    assert "policyLines" in g, "revision 计算里没有策略项"
    i = g.index("def revLines")
    block = g[i:i + 400]
    assert "policyLines" in block, (
        "revision 的输入必须包含策略行（disable 列表），实际：%s" % block[:200])
