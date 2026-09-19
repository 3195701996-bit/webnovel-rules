#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""拷贝漫画 (copy4000.com) 网页通道 —— 纯 HTTP（无浏览器）取数探针。

研究结论落地脚本：复现网页 JS 的解密逻辑，直接以 requests 取
`GET https://copy4000.com/comicdetail/<path_word>/chapters` 并在本地解密。

算法（实测 + JS 反混淆双向确认）
--------------------------------
源文件: static/websitefree/js20190704/comic_detail_pass202508141558.js
        （Dean-Edwards 打包 + javascript-obfuscator 字符串表混淆，已还原）
        （本地还原件在 /tmp/copy_js/deob.js，本次研究临时目录）

    var ccz     = <详情页 HTML 内联注入的 16 字符密钥>;      // 例: 'op0zzpvv.nmn.00p'
    var dntsHdr = document.querySelector('#dnt').getAttribute('value');   // 例: "3"
    request({
        type: 'GET',
        headers: { 'dnts': dntsHdr },                        // 站点自定义反爬头
        url: location.origin + '/comicdetail/' + pathWord + '/chapters',
        success: function (res) {
            var k = CryptoJS.enc.Utf8.parse(ccz);                          // KEY
            var iv = CryptoJS.enc.Utf8.parse(res.results.substring(0,16));  // IV = 前 16 字符
            var ct = res.results.substring(16);                            // 余下为 hex 密文
            var hexWA = CryptoJS.enc.Hex.parse(ct);
            var pt = CryptoJS.AES.decrypt(
                        CryptoJS.enc.Base64.stringify(hexWA), k,
                        { iv: iv, mode: CryptoJS.mode.CBC, padding: CryptoJS.pad.Pkcs7 }
                     ).toString(CryptoJS.enc.Utf8);
            var json = JSON.parse(pt);
        }
    });

即 AES-128-CBC / PKCS7，key = UTF8(ccz)，iv = UTF8(results[:16])，
密文 = bytes.fromhex(results[16:])。密钥来自页面 HTML（每页内联），不是 URL/时间派生。

用法
----
    ./venv/bin/python tools/copy_web_probe.py --path-word shenmingyuchulian
    ./venv/bin/python tools/copy_web_probe.py --path-word jurenmeiman --compare-dnts

纪律
----
* 每次运行最多 3 个 HTTP 请求（内置硬断言，超限直接抛错）。
* 不写任何业务目录，只往 stdout 打印；失败时原样打印状态码与响应片段。
* 不重试、不并发、不做批量；遇 403 / Cloudflare 挑战立即停止并如实报告。
"""

from __future__ import annotations

import argparse
import json
import re
import sys

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BASE = "https://copy4000.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MAX_REQUESTS_PER_RUN = 3


class Budget:
    """请求计数器：守住每次运行 <= 3 次。"""

    def __init__(self, limit: int = MAX_REQUESTS_PER_RUN) -> None:
        self.limit = limit
        self.used = 0

    def spend(self) -> None:
        if self.used + 1 > self.limit:
            raise RuntimeError(
                "request budget exceeded: %d/%d" % (self.used + 1, self.limit)
            )
        self.used += 1


def aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """AES-CBC + PKCS7 解密（cryptography 后端，venv 已内置）。"""
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    data = dec.update(ciphertext) + dec.finalize()
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        data = data[:-pad]
    return data


def unwrap(results: str, key_text: str) -> dict:
    """复现 JS：results = 16 字符 IV(明文 UTF8) + hex 密文。"""
    if len(results) < 32:
        raise ValueError("results too short: %d chars" % len(results))
    iv = results[:16].encode("utf-8")
    ciphertext = bytes.fromhex(results[16:])
    plain = aes_cbc_decrypt(key_text.encode("utf-8"), iv, ciphertext)
    # JS 侧是 JSON.parse(...trim())，这里同样容错首尾空白
    return json.loads(plain.decode("utf-8").strip())


def probe_bytes(resp: requests.Response, n: int = 240) -> str:
    """失败时原样打印响应片段（不贴 Cookie/token）。"""
    body = resp.content[:n]
    try:
        return repr(body.decode("utf-8", "replace"))
    except Exception:  # pragma: no cover
        return repr(body)


def fetch_page_key(session: requests.Session, path_word: str, budget: Budget):
    """取详情页 HTML，抽取内联密钥 ccz 与反爬头 #dnt 的 value。"""
    url = "%s/comic/%s" % (BASE, path_word)
    budget.spend()
    resp = session.get(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        timeout=25,
    )
    print("[req] GET %s -> HTTP %s (%d bytes)" % (url, resp.status_code, len(resp.content)))
    if resp.status_code != 200:
        print("       body: %s" % probe_bytes(resp))
        return None, None
    html = resp.text
    key_match = re.search(r"var\s+ccz\s*=\s*'([^']+)'", html)
    dnt_match = re.search(r'<span[^>]*id="dnt"[^>]*value="([^"]*)"', html)
    key = key_match.group(1) if key_match else None
    dnt = dnt_match.group(1) if dnt_match else None
    print("       inline ccz key = %r" % key)
    print("       #dnt value     = %r" % dnt)
    return key, dnt


def fetch_chapters(
    session: requests.Session,
    path_word: str,
    budget: Budget,
    dnt: str | None = None,
):
    """取加密的章节响应；dnt 非 None 时带上站点自定义反爬头 'dnts'。"""
    url = "%s/comicdetail/%s/chapters" % (BASE, path_word)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "%s/comic/%s" % (BASE, path_word),
    }
    if dnt is not None:
        headers["dnts"] = str(dnt)
    budget.spend()
    resp = session.get(url, headers=headers, timeout=25)
    label = "with dnts" if dnt is not None else "no dnts"
    print(
        "[req] GET %s (%s) -> HTTP %s (%d bytes)"
        % (url, label, resp.status_code, len(resp.content))
    )
    if resp.status_code != 200:
        print("       body: %s" % probe_bytes(resp))
        return None
    return resp


def describe(payload: dict, path_word: str) -> str:
    """只打印前几条章节，避免刷屏。"""
    groups = payload.get("groups") or {}
    build = payload.get("build") or {}
    lines = []
    lines.append("       build.path_word = %r" % build.get("path_word"))
    lines.append("       build.type      = %s" % json.dumps(build.get("type"), ensure_ascii=False))
    total = 0
    for gname, gval in groups.items():
        chapters = gval.get("chapters") or []
        total += len(chapters)
        lines.append(
            "       group %-12s count=%s chapters=%d"
            % (gname, gval.get("count"), len(chapters))
        )
        for ch in chapters[:3]:
            lines.append(
                "         - id=%s name=%s index=%s size=%s"
                % (ch.get("id"), ch.get("name"), ch.get("index"), ch.get("size"))
            )
    lines.append("       TOTAL chapters returned = %d" % total)
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="copy4000 网页通道纯 HTTP 取数探针")
    ap.add_argument("--path-word", default="shenmingyuchulian", help="漫画 path_word")
    ap.add_argument("--raw", action="store_true", help="额外打印密文前缀（便于排查）")
    ap.add_argument(
        "--compare-dnts",
        action="store_true",
        help="A/B 对比：同一端点分别在不带/带 'dnts' 反爬头下取一次（总请求 3 次）",
    )
    args = ap.parse_args(argv)

    budget = Budget()
    session = requests.Session()
    session.headers["User-Agent"] = UA

    key, dnt = fetch_page_key(session, args.path_word, budget)
    if not key:
        print("[!] 未能从详情页拿到内联密钥 ccz —— 页面结构可能已改，停止。")
        return 2

    results = []
    plan = [None] + ([dnt] if args.compare_dnts else [])
    for dnt_val in plan:
        resp = fetch_chapters(session, args.path_word, budget, dnt=dnt_val)
        if resp is None:
            results.append((dnt_val, None, "http error"))
            continue
        try:
            body = resp.json()
        except Exception as exc:  # 非 JSON（可能是 CF 挑战页 / 风控原文）
            print("       body: %s" % probe_bytes(resp))
            results.append((dnt_val, None, "not json: %s" % exc))
            continue
        if body.get("code") != 200 or "results" not in body:
            print("       body: %s" % json.dumps(body, ensure_ascii=False)[:300])
            results.append((dnt_val, None, "code=%s" % body.get("code")))
            continue
        enc = body["results"]
        print("       results: %d chars (iv=%r, ct=%d bytes)"
              % (len(enc), enc[:16], (len(enc) - 16) // 2))
        if args.raw:
            print("       raw prefix: %s..." % enc[:80])
        try:
            payload = unwrap(enc, key)
        except Exception as exc:
            results.append((dnt_val, None, "decrypt failed: %r" % (exc,)))
            print("[!] 解密失败: %r" % (exc,))
            continue
        print("       ---- decrypted JSON ----")
        print(describe(payload, args.path_word))
        results.append((dnt_val, payload, "ok"))

    print()
    print("== 汇总 (path_word=%s, 本次请求数=%d) ==" % (args.path_word, budget.used))
    any_ok = False
    for dnt_val, payload, status in results:
        label = "dnts=%s" % dnt_val if dnt_val is not None else "dnts=<absent>"
        if payload is None:
            print("  %-22s %s" % (label, status))
            continue
        any_ok = True
        groups = payload.get("groups") or {}
        total = sum(len((g.get("chapters") or [])) for g in groups.values())
        print("  %-22s ok, chapters=%d" % (label, total))
    # 全部请求失败才算失败；单次失败已在上面原样打印状态码与响应片段。
    return 0 if any_ok else 3


if __name__ == "__main__":
    sys.exit(main())
