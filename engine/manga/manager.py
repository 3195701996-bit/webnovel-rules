# -*- coding: utf-8 -*-
"""漫画源注册表：key → 适配器类/实例"""
import threading

_REGISTRY = {}
_LOCK = threading.Lock()


def register_adapter(cls):
    """注册适配器类（装饰器或直接调用）"""
    with _LOCK:
        _REGISTRY[cls.key] = cls
    return cls


# R59 曾把 "copymanga" 键映射到 Playwright 渲染通道（copymanga_web），
# 目的是"不再发出 APP 接口请求特征"（210 风控源头）。
#
# **0.74.3 起取消这个映射**，理由（都是实测）：
#   1. R59 的目的已经由新通道达成：`copymanga` 现在的主通道是 **纯 HTTP 网页通道**
#      （engine/manga/copy_web.py，无浏览器/无 Cookie/无登录），APP 接口只在
#      网页通道出**解析类**错误时才回落，网络不通时直接不回落；
#   2. 映射让**桌面比手机慢十倍**：同一部作品，Playwright 渲染 9.6s 搜索 / 19.2s 详情，
#      而纯 HTTP 通道 2.4s / 1.7s（2026-09-17 实测）；
#   3. 更糟的是**证据失真**：桌面跑「逐源校验」时测的是 Playwright 通道（手机根本没有），
#      得到的结论无法代表手机——这是指南 §2.5 明令禁止的"用不是该环境的通道得出结论"。
# 保留 copymanga_web 作为**独立可选项**（桌面用户想用渲染通道时仍可显式选它），
# 但不再劫持 "copymanga" 这个键。
_APP_FORBIDDEN = {}


def get_adapter(key, state_dir=None):
    """获取适配器实例（每次新建实例；状态目录用于持久化）"""
    key = _APP_FORBIDDEN.get(key, key)
    with _LOCK:
        cls = _REGISTRY.get(key)
    if not cls:
        return None
    return cls(state_dir=state_dir) if state_dir is not None else cls()


def adapter_class(key):
    """按 key 取**适配器类**（不实例化）"""
    key = _APP_FORBIDDEN.get(key, key)
    with _LOCK:
        return _REGISTRY.get(key)


def adapter_meta(key):
    """适配器的静态能力元信息（**不实例化**）。

    为什么强调不实例化（2026-09-15 实测）：`server.state._manga_adapter` 是**单例缓存**，
    为了展示"是否需要块还原"而顺手实例化适配器，会把真实实例写进那份缓存，
    于是别的流程（如章节修复覆盖用例里替换 get_adapter 的假适配器）拿到的是真实实例，
    行为与预期不符、还会在暂存目录留下残留。这类"只读能力查询"必须走类属性。
    """
    cls = adapter_class(key)
    if cls is None:
        return {"registered": False, "scrambled": False, "process_version": 0}
    try:
        ver = int(getattr(cls, "PROCESS_VERSION", 0) or 0)
    except (TypeError, ValueError):
        ver = 0
    # 排序参数支持：与 server.manga_api._supports_order 同口径（看 search 签名），
    # 但这里只看**类**——绝不为了读元信息而实例化适配器（见上面的注释）
    supports_order = False
    try:
        import inspect as _inspect
        supports_order = "order" in _inspect.signature(cls.search).parameters
    except (TypeError, ValueError, AttributeError):
        supports_order = False
    return {"registered": True,
            "scrambled": bool(getattr(cls, "unscramble_image", None)),
            "supports_order": supports_order,
            "process_version": ver}


def list_adapters():
    with _LOCK:
        return [{"key": k, "name": v.name, "version": v.version} for k, v in _REGISTRY.items()]
