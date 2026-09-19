# -*- coding: utf-8 -*-
"""R78 回归：客户端解析器 ↔ 服务端响应的字段契约（工具守卫）。

`tools/check_client_server_contract.py` 从 EngineData.kt 里按**数据流分层**抽出
每个解析函数读的键（顶层 / 数组元素 / 嵌套对象），再对真实响应逐个核对。
字段漂移不会报错，只会让客户端静默拿到默认值（数字变 0、文案变空）——
本项目已经因此踩过 5 起：`idx` 写成 `index`、`timed_out_sources` 顶替 `done`、
`missing_count` 没被用、图片扩展名少一个、小说任务进度用了 `completed` 而客户端读 `done`。

这里把工具当守卫跑：**发现任何未登记的缺失键就失败**。
（工具自身有 CONDITIONAL 表登记"只在特定状态才出现的键"，每条都写明原因；
没写原因的不许进表——否则工具会退化成橡皮图章。）
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_client_parser_keys_all_present_in_server_payload():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools",
                                                     "check_client_server_contract.py")],
                       cwd=ROOT, capture_output=True, text=True, timeout=600, check=False,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                            "WR_TEST": "1", "WR_DISABLE_BACKGROUND": "1"})
    tail = "\n".join(r.stdout.strip().split("\n")[-6:])
    assert r.returncode == 0, f"发现字段契约漂移：\n{tail}\n\n{r.stderr[-800:]}"
    assert "发现问题 0 个" in r.stdout, tail


# ── 解析器精度与覆盖面（都是被真事故逼出来的判据）─────────────────────

def _req(fn):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import check_client_server_contract as C
    return C, C.parse_requirements(fn)


def test_value_arrays_do_not_become_bogus_keys():
    """`arr.optString(it)` 是取**元素的值**，不是读键名。

    第一版把 `it` 当成键名，于是给这些接口加核对时会报出根本不存在的"缺失键 it"
    —— 噪声，而工具一旦噪报就没人信。
    """
    _C, r = _req("mangaDetail")
    flat = [k for ks in r["array"].values() for k in ks]
    assert not any(k.split(".")[-1] in ("i", "it", "j") for k in flat), r
    assert r.get("unresolved") == [], r


def test_nested_array_keeps_parent_path():
    """`val arr = o.optJSONArray("sources")` 而 o 是 `optJSONObject("manga")`
    → 真实路径 `manga.sources`。记成顶层 `sources` 会去顶层找、找不到就"跳过"
    （看起来是跳过，其实是漏检）。"""
    _C, r = _req("exploreMangaSources")
    assert "manga.sources" in r["array"], r["array"]


def test_json_parsers_detection_excludes_helpers():
    """覆盖面统计只算"真的在读 JSON"的函数：`mangaPos` 这类格式化函数不算，
    否则覆盖率会虚高。"""
    C = _req("mangaPos")[0]
    parsers = C._json_parsers()
    assert "engineSummary" in parsers and "mangaDetail" in parsers
    assert "mangaPos" not in parsers and "pageFromPos" not in parsers


def test_every_json_parser_is_accounted_for():
    """**覆盖面不变式**：每个读 JSON 的解析函数，要么走接口核对、要么走别的通道、
    要么在 SKIPPED_PARSERS 写明原因。没有这条，接口改名/新增解析函数时
    "没被覆盖"会以"没人发现"的方式发生。"""
    C = _req("engineSummary")[0]
    parsers = C._json_parsers()
    assert len(parsers) >= 27, f"解析函数数量异常减少（{len(parsers)}）：{sorted(parsers)}"
    unaccounted = sorted(f for f in parsers
                         if f not in C.ENDPOINTS and f not in C.SKIPPED_PARSERS
                         and f not in C.COVERED_ELSEWHERE)
    assert not unaccounted, f"这些解析函数没被核对也没登记原因：{unaccounted}"
    stale = sorted(f for f in list(C.SKIPPED_PARSERS) + list(C.COVERED_ELSEWHERE)
                   if f not in parsers)
    assert not stale, f"登记表里有不存在（或不是解析函数）的名字：{stale}"
    # 登记跳过必须写明理由，含糊不许进表
    for fn, why in list(C.SKIPPED_PARSERS.items()) + list(C.COVERED_ELSEWHERE.items()):
        assert len(why) >= 12, f"{fn} 的理由太短，等于没写：{why}"


def test_dig_resolves_dotted_paths():
    C = _req("engineSummary")[0]
    payload = {"runtime": {"state": "running", "wsgi": "waitress"}}
    assert C._dig(payload, "runtime.state") == "running"
    assert C._dig(payload, "runtime.missing") is None
    assert C._dig(payload, "nope.deep") is None


def test_null_value_is_not_a_missing_path():
    """服务端明确发 `"progress": null` 与**根本没有这个键**是两件事：
    前者客户端有兜底（`?: JSONObject()`），后者是改名/漏发。
    分不开就会一边报假缺失、一边把真漏检当"夹具没构造"跳过。"""
    C = _req("engineSummary")[0]
    assert C._dig2({"progress": None}, "progress") == (None, False)
    assert C._dig2({}, "progress") == (None, True)
    assert C._dig2({"a": {"b": None}}, "a.b") == (None, False)
    assert C._dig2({"a": {}}, "a.b") == (None, True)


def test_two_arg_read_is_captured():
    """`p.optInt("completed", p.optInt("done"))`（带默认值）必须被抽到。

    第一版的正则只认单参数写法 → `completed` **从来没被核对过**：
    把引擎里的 `'completed'` 改名成 `'completedX'` 都能通过（变异验证抓出来的）。
    """
    _C, r = _req("tasks")
    keys = r["array"].get("tasks") or []
    assert "progress.completed" in keys, keys


def test_element_nested_object_stays_in_array_layer():
    """`val p = o.optJSONObject("progress")` 里 o 是 `tasks[]` 的元素
    → 是 `tasks[].progress`，不是顶层 `progress`。这条要靠**迭代到不动点**推导
    （数组 → 元素 → 嵌套对象三层依赖），单趟扫描会断链、报"响应里没有 progress"。"""
    _C, r = _req("tasks")
    assert "progress" not in r["object"], r["object"]
    assert any(k.startswith("progress.") for k in r["array"].get("tasks") or [])


def test_proxy_state_exposes_both_sides():
    """指南 P1-2：漫画侧与**小说侧**代理是两套实现，必须**分别可见**。

    小说侧优先级（engine/fetcher.py）：书源 `proxy` 字段 > `WR_PROXY` > `data/proxies.txt` 池
    （只对直连失败过的域名启用）。以前 `/api/net/proxy` 只报漫画侧那一个开关，
    用户看不到小说侧走的是什么，也无从自查。
    """
    import json
    import os
    import subprocess
    code = (
        "import os,sys,json;"
        "os.environ.setdefault('PORT','18997');"
        "sys.path.insert(0,'.');"
        "import app as m;"
        "d=m.app.test_client().get('/api/net/proxy').get_json()['data'];"
        "nv=d.get('novel') or {};"
        "print(json.dumps({'keys':sorted(nv),'mode':nv.get('mode')}))"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                       text=True, timeout=120, check=False,
                       env={**os.environ, "WR_PROFILE": "mobile", "WR_TEST": "1",
                            "WR_DISABLE_BACKGROUND": "1", "PORT": "18997"})
    assert r.returncode == 0, r.stderr[-400:]
    d = json.loads(r.stdout.strip().split("\n")[-1])
    for k in ("mode", "env_proxy", "pool_size", "good_count", "note"):
        assert k in d["keys"], f"小说侧代理状态缺 {k}：{d['keys']}"
    assert d["mode"] in ("direct", "env", "pool"), d
