# -*- coding: utf-8 -*-
"""禁漫 `_get` 的两条出口必须同口径（0.74.3 实测到的真 bug）。

## 缺陷

`_get` 正常路径返回**解密后的明文 JSON 文本**（str），而"换服务器重试"路径
直接 `return body`（未解密的信封 dict）。于是：

- 类型不一致 → 调用方 `json.loads(self._get(...))` 撞上 dict →
  `TypeError: the JSON object must be str, bytes or bytearray, not dict`；
- 内容也不一致 → 那是**密文信封**，就算类型对了也拿不到数据。

触发条件很常见：**首台服务器慢/失败**（实测常见，禁漫有 4 台以上可用域），
用户侧表现就是"刚才还能看，突然整章图片/目录取不到"。

## 本文件锁死

换服务器重试后，返回的必须是**与正常路径同类型、同内容**的明文 JSON 文本。
"""
import base64
import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.manga import jm as JM  # noqa: E402

FIXED_T = 1770000000
SECRET = "diosfjckwpqpdfjkvnqQjsik"


def _encrypt(plain, t):
    """按 _aes_ecb_decrypt 的逆运算造信封（测试自造向量，不依赖源站）"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = hashlib.md5(("%d%s" % (t, JM.RESP_SECRET)).encode()).hexdigest().encode()
    body = plain.encode("utf-8")
    pad = 16 - len(body) % 16
    body += bytes([pad]) * pad                       # PKCS7
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return base64.b64encode(enc.update(body) + enc.finalize()).decode()


class _Resp(object):
    def __init__(self, status, payload):
        self.status_code = status
        self.content = json.dumps(payload).encode("utf-8")


class _Sess(object):
    """第一次请求失败（模拟慢/坏服务器），换域后成功"""

    def __init__(self):
        self.n = 0
        self.urls = []

    def get(self, url, **kw):
        self.n += 1
        self.urls.append(url)
        if self.n == 1:
            raise ConnectionError("首台服务器无响应")
        return _Resp(200, {"data": _encrypt('{"content":[{"id":"1"}]}', FIXED_T)})


def _bare_adapter(monkeypatch, sess):
    """**不调用构造函数**（它会联网拉服务器列表/图片域，测试里会真的打源站）。

    只装上 `_get` 真正用到的那几个属性。
    """
    monkeypatch.setattr(JM.time, "time", lambda: FIXED_T)
    monkeypatch.setattr(JM, "_jm_api_session", lambda: sess)
    ad = JM.Jm.__new__(JM.Jm)
    ad._servers = ["first.example"]
    ad._img_domain = "cdn.example"
    ad._note_server = lambda *a, **k: None
    ad._rotate_server = lambda host: "other.example"
    ad.refresh_domains = lambda *a, **k: False
    ad._save_state = lambda *a, **k: None
    return ad


def test_rotate_retry_returns_decrypted_text_like_normal_path(monkeypatch):
    ad = _bare_adapter(monkeypatch, _Sess())
    out = ad._get("https://first.example/search?x=1", timeout=3)
    assert isinstance(out, str), (
        "换服务器重试路径必须返回明文文本（原实现返回未解密的 dict，"
        "调用方 json.loads 直接 TypeError）")
    assert json.loads(out)["content"][0]["id"] == "1", out


def test_normal_path_and_retry_path_have_same_type(monkeypatch):
    """两条出口同类型：正常路径 str，换域重试路径也必须 str"""

    class _Ok(_Sess):
        def get(self, url, **kw):
            self.n += 1
            return _Resp(200, {"data": _encrypt('{"content":[]}', FIXED_T)})
    ad = _bare_adapter(monkeypatch, _Ok())
    normal = ad._get("https://first.example/a", timeout=3)
    ad2 = _bare_adapter(monkeypatch, _Sess())
    retried = ad2._get("https://first.example/b", timeout=3)
    assert type(normal) is type(retried) is str


def test_rotate_retry_without_data_field_raises_manga_error(monkeypatch):
    """信封里没有 data（源站格式变了）→ 明确报错，不许把 dict 漏出去"""

    class _S(_Sess):
        def get(self, url, **kw):
            self.n += 1
            if self.n == 1:
                raise ConnectionError("首台服务器无响应")
            return _Resp(200, {"unexpected": True})
    ad = _bare_adapter(monkeypatch, _S())
    with pytest.raises(JM.MangaError) as ei:
        ad._get("https://first.example/x", timeout=3)
    assert "格式异常" in str(ei.value)
