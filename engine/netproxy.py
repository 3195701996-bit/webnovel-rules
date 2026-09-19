# -*- coding: utf-8 -*-
"""出站代理（0.73.0）：**单一事实来源**，供所有联网通道读取。

## 为什么需要

真机诊断（2026-09-17，vivo V2304A / Android 16）显示：API 三段已经很快
（搜索 1222ms / 详情 478ms / 章节 317ms），但**首图 4995ms**；同时
copy4000 / baozimh / nhentai 等域在开发网络下直连被**连接重置**、走代理即恢复
（包子漫画同一 URL：直连两条传输都失败、走代理两条都 200）。也就是说
"每源降级"的主因是**网络可达性**，用户已确认要"自定义代理"。

## 设计

- 优先级：`WR_PROXY` 环境变量 > 应用私有目录里的配置文件（`WR_PROXY_FILE`，
  手机端由 App 的"网络 → 代理"写入）> 无代理（直连）。
- 只认 `http://` / `socks5://`(含 `socks5h`) 形式；非法值**忽略并记日志**，
  绝不"看起来配了代理其实直连"。
- `set_proxy()` 会**重置各通道的会话缓存**（图片池、jm API 会话、copymanga 会话），
  否则旧会话会继续用旧路径，表现成"改了代理没生效"。

## 安全边界（必须写清，别让读者误解）

代理只是**网络路径**：它不绕过任何站点的技术保护，也不改变源站自己的风控
（例如拷贝漫画的 210"破解版"标记与代理无关）。经代理时，连接终点是代理而不是
目标站，因此**目标 IP 绑定（防 DNS 重绑定）不再适用**——调用方据此跳过绑定，
其余 SSRF 校验（协议、字面内网地址、逐跳重定向）一律照旧。
"""
import os
import threading
import time

_LOCK = threading.Lock()
_cached = None            # (value, mtime_of_file)
_env_pushed = None        # set_proxy 自己推进环境变量的值（用于区分"用户设的环境变量"）
# 代理可达性（0.74.2）：只做**一次 TCP 连接**探测，结果缓存 HEALTH_TTL 秒。
# 起因是真机事故：用户按上一版说明把代理指向电脑的局域网地址，之后电脑关机/
# 不在同一 Wi-Fi，于是**所有出站请求都发往一个死地址 → 每个源都"超时"**，
# 而且界面上看不出原因（它只显示"经代理"）。宁可多花 2 秒探一次，也不能让
# "一个过期的设置"把整个 App 变成不可用。
_HEALTH = {"value": None, "ts": 0.0, "ok": True, "err": "", "fell_back": False}
HEALTH_TTL = 30.0
# 默认只在手机档（WR_PROFILE=mobile）开启：桌面用户跑测试/脚本时不该被额外探测干扰，
# 需要时用 WR_PROXY_HEALTHCHECK=1/0 显式覆盖。
def _health_enabled():
    v = (os.environ.get("WR_PROXY_HEALTHCHECK") or "").strip()
    if v in ("1", "true", "on"):
        return True
    if v in ("0", "false", "off"):
        return False
    return (os.environ.get("WR_PROFILE") or "").strip().lower() == "mobile"


def _proxy_host_port(value):
    """代理串 → (host, port)；解析不出来返回 (None, None)"""
    s = str(value or "").strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    if "@" in s:
        s = s.rsplit("@", 1)[-1]
    s = s.split("/", 1)[0]
    if s.startswith("["):                       # [::1]:7897
        host, _, rest = s[1:].partition("]")
        port = rest.lstrip(":")
    elif ":" in s:
        host, port = s.rsplit(":", 1)
    else:
        host, port = s, ""
    try:
        return host.strip(), int(port or 0)
    except ValueError:
        return host.strip(), 0


def _probe(value, timeout=2.0):
    """TCP 连一下代理：能连上就算可达。返回 (ok, err)。绝不抛异常。"""
    host, port = _proxy_host_port(value)
    if not host or not port:
        return False, "代理地址无法解析出主机:端口"
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except Exception as e:                                  # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, str(e)[:80])


def healthy_proxy(value=None):
    """带缓存的代理可达性判定 → (生效值, 健康信息)。

    不可达时**返回空串**（本次直连）并把原因记进 health()，由界面/诊断如实展示：
    "你配的代理连不上，已临时直连（地址仍保留）"——不是静默忽略用户设置。
    """
    val = current_proxy_raw() if value is None else _valid(value)
    if not val or not _health_enabled():
        return val, {"configured": val, "ok": True, "checked": False,
                     "err": "", "fell_back": False}
    now = time.time()
    with _LOCK:
        h = dict(_HEALTH)
    if h.get("value") == val and now - h.get("ts", 0.0) < HEALTH_TTL:
        return ("" if h["fell_back"] else val), _health_view(val, h)
    ok, err = _probe(val)
    with _LOCK:
        _HEALTH.update({"value": val, "ts": now, "ok": ok, "err": err,
                        "fell_back": not ok})
    if not ok:
        print("[netproxy] ⚠ 代理不可达（%s），本次直连：%s"
              % (redact_proxy(val), err), flush=True)
    return (val if ok else ""), _health_view(val, dict(_HEALTH))


def _health_view(val, h):
    return {"configured": val, "ok": bool(h.get("ok", True)),
            "checked": bool(h.get("ts")),
            "err": h.get("err") or "",
            "fell_back": bool(h.get("fell_back")),
            "checked_at": h.get("ts") or 0.0,
            "stale": bool(h.get("value") and h.get("value") != val)}


def health():
    """当前代理的健康状况（给界面/诊断用；不做新探测）"""
    val = current_proxy_raw()
    with _LOCK:
        h = dict(_HEALTH)
    view = _health_view(val, h)
    view["enabled"] = _health_enabled()
    return view


def _valid(proxy):
    if not proxy:
        return ""
    p = str(proxy).strip()
    if not p or p.lower() in ("none", "off", "direct", "0"):
        return ""
    if "://" not in p:
        p = "http://" + p
    scheme = p.split("://", 1)[0].lower()
    if scheme not in ("http", "https", "socks5", "socks5h", "socks4"):
        return ""
    return p


def config_path():
    """应用私有目录里的代理配置文件（手机端由设置页写入；桌面端可不设）。"""
    return (os.environ.get("WR_PROXY_FILE") or "").strip()


def current_proxy_raw():
    """**纯读取**当前配置的代理（不看健康）。环境变量优先，其次配置文件。"""
    global _cached
    env = _valid(os.environ.get("WR_PROXY"))
    if env:
        return env
    path = config_path()
    if not path:
        return ""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    with _LOCK:
        if _cached and _cached[1] == mtime:
            return _cached[0]
    val = ""
    try:
        with open(path, encoding="utf-8") as f:
            val = _valid(f.readline())
    except Exception:
        val = ""
    with _LOCK:
        _cached = (val, mtime)
    return val


def current_proxy():
    """**实际生效**的代理（无则空串）。

    与 `current_proxy_raw()` 的区别：这里会判可达性——配了但连不上时返回空串
    （本次走直连），原因见 `health()`。这是为了让"一个过期/死掉的代理设置"
    不会把整个 App 变成"每个源都超时"。
    """
    val, _h = healthy_proxy()
    return val


def proxy_dict(url=""):
    """requests/curl_cffi 通用写法：{'http': p, 'https': p}；无代理返回 None。"""
    p = current_proxy()
    if not p:
        return None
    return {"http": p, "https": p}


def is_proxied():
    return bool(current_proxy())


def source():
    """**实际生效**值的来源：`env` / `set` / `file` / `direct`。

    说清"这个代理是谁设的"，否则用户会以为被环境变量覆盖了。
    `set` 的存在是必要的：设置页写入时既要落盘、又要立刻生效，于是同时推了
    环境变量；若不区分，界面会把"我刚保存的"显示成"环境变量优先"，看着像被
    别人改过（实测就是这样误导排查的）。
    代理配了但**不可达**时这里返回 `direct`（本次真的在直连），原因见 `health()`。
    """
    if not current_proxy():
        return "direct"
    env = _valid(os.environ.get("WR_PROXY"))
    if not env:
        return "file"
    if _env_pushed and _valid(_env_pushed) == env:
        return "set"
    return "env"


def redact_proxy(value):
    """把代理串里的凭据抹掉（报告/日志用）：`socks5://u:p@h:1080` → `socks5://h:1080`。

    诊断报告是要发给别人看的（用户截图/导出），代理串里可能带用户名密码，
    因此凡是要走出去的地方都过这里；本机 API 回显用户自己填的值时不过滤
    （否则用户无法确认是否填对）。
    """
    s = str(value or "").strip()
    if not s:
        return ""
    if "@" in s and "://" in s:
        head, tail = s.split("://", 1)
        return "%s://%s" % (head, tail.rsplit("@", 1)[-1])
    return s


def reset_channel_sessions():
    """切换代理后重置各通道会话（图片池 / jm API / copymanga）。返回重置项数。"""
    n = 0
    try:
        import engine.manga.downloader as dl
        for fn in ("_reset_pool", "_reset_sessions"):
            f = getattr(dl, fn, None)
            if callable(f):
                f()
                n += 1
                break
        else:
            pool = getattr(dl, "_session_pool", None)
            if isinstance(pool, list) and hasattr(pool, "clear"):
                for s in list(pool):
                    try:
                        s.close()
                    except Exception:
                        pass
                pool.clear()
                n += 1
    except Exception:
        pass
    try:
        import engine.manga.jm as jm
        f = getattr(jm, "_jm_api_reset", None)
        if callable(f):
            f()
            n += 1
    except Exception:
        pass
    try:
        import engine.manga.copymanga as cm
        f = getattr(cm, "_reset_session", None)
        if callable(f):
            f()
            n += 1
    except Exception:
        pass
    return n


def set_proxy(value, persist=True):
    """设置/清除代理：写配置文件（可选）+ 设环境变量 + 重置会话。

    返回生效后的代理字符串（空串 = 直连）。
    """
    global _cached, _env_pushed
    val = _valid(value)
    path = config_path()
    if persist and path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(val + "\n")
            os.replace(tmp, path)
        except Exception as e:                                  # noqa: BLE001
            print("[netproxy] 代理配置写入失败: %s: %s" % (type(e).__name__, e),
                  flush=True)
    if val:
        os.environ["WR_PROXY"] = val
        _env_pushed = val
    else:
        os.environ.pop("WR_PROXY", None)
        _env_pushed = None
    with _LOCK:
        _cached = None
    reset_channel_sessions()
    return val
