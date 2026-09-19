# -*- coding: utf-8 -*-
"""禁漫（jm）图片块还原参数的确定性回归（离线，不触网）

来源：0.51.0 风险评估 P0-1 —— 用户截图上"条带状/拼图式错位"与 jm 图片块还原参数
计算错误相符。旧实现：

    picture_name = image_url.rstrip("/").split("/")[-1][:-5]

按"扩展名恒为 5 个字符"切片，于是：
  · `00001.jpg`（4 字符扩展名）→ 取到 `0000`（**连最后一位数字一起切掉**）；
  · `00001.jpg?token=x` → 把 query 也当成文件名一部分；
  · 块数算错 → jm.py 的倒序拼接按错误边界执行 → 图片错位。
修好后这三者与 `.webp` 一样得到正确 basename，ep=300000 时块数都是 16。

本用例锁住：
  1. basename 计算：扩展名 / query / fragment / 百分号编码都要正确；
  2. 已知向量：ep=300000 → 16 块（旧实现在 .jpg 上给 6、带 query 给 18）；
  3. 阈值行为：ep < SCRAMBLE_ID → 0 块；SCRAMBLE_ID..268850 → 10 块；
  4. "整本相册"（详情无 series，chapter_id 是相册 id）**照常做块还原**（0.65.0 修：
     以前这里直接跳过还原，导致下载下来的单话相册永远是乱序图且不报错）；
  5. 还原失败必须**抛出**（旧实现 return 原字节 → 乱序图被当成功写进缓存）；
  6. 合成图往返：先用已知块数打乱，再还原，逐像素等于原图。
"""
import hashlib
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.manga.jm import Jm, SCRAMBLE_ID, PROCESS_VERSION  # noqa: E402
from engine.manga.base import MangaError  # noqa: E402

IMG = "https://cdn.example/media/photos/300000/"


def _adapter():
    """不联网的适配器实例：只测纯函数（__new__ 跳过 __init__ 的网络/文件副作用）"""
    return Jm.__new__(Jm)


# ── 1) basename 计算 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("url, want", [
    (IMG + "00001.jpg", "00001"),
    (IMG + "00001.jpeg", "00001"),
    (IMG + "00001.png", "00001"),
    (IMG + "00001.webp", "00001"),
    (IMG + "00001.jpg?token=abc", "00001"),
    (IMG + "00001.jpg#frag", "00001"),
    (IMG + "00001.webp?v=2#x", "00001"),
    ("https://cdn.example/a/b/00012.PNG", "00012"),
])
def test_picture_name_handles_extensions_and_query(url, want):
    assert Jm.picture_name(url) == want


# ── 2) 已知向量（旧实现会算错）────────────────────────────────────────────

def test_known_vector_ep_300000_is_16_blocks():
    ad = _adapter()
    for url in (IMG + "00001.jpg", IMG + "00001.webp",
                IMG + "00001.jpg?token=x", IMG + "00001.jpeg"):
        assert ad.image_scramble_num(300000, url) == 16, url


def test_old_implementation_would_have_been_wrong():
    """把旧算法写出来对照：证明这个向量确实能区分修好与否（不是自我证明）"""
    ep = 300000
    name = "00001"
    old_name = (IMG + "00001.jpg").rstrip("/").split("/")[-1][:-5]      # 旧
    assert old_name == "0000", "旧实现会把最后一位数字切掉"
    h_old = hashlib.md5(f"{ep}{old_name}".encode()).hexdigest()
    h_new = hashlib.md5(f"{ep}{name}".encode()).hexdigest()
    old_blocks = ord(h_old[-1]) % 10 * 2 + 2
    assert old_blocks != 16 and old_blocks == 6, f"旧实现应得 6 块（实测 {old_blocks}）"
    assert ord(h_new[-1]) % 10 * 2 + 2 == 16


# ── 3) 阈值行为 ──────────────────────────────────────────────────────────

def test_thresholds():
    ad = _adapter()
    assert ad.image_scramble_num(SCRAMBLE_ID - 1, IMG + "00001.jpg") == 0
    assert ad.image_scramble_num(SCRAMBLE_ID, IMG + "00001.jpg") == 10
    assert ad.image_scramble_num(268849, IMG + "00001.jpg") == 10
    assert ad.image_scramble_num(268850, IMG + "00001.jpg") % 2 == 0


# ── 4) 整本相册（无 series）不参与块还原 ───────────────────────────────────

def test_album_only_chapter_is_still_unscrambled():
    """整本相册（无 series）**也必须还原** —— 0.65.0 修复。

    旧行为：登记为"相册型"后 `image_scramble_num` 直接返回 0 = 不做块还原。
    下载流程恰好先取详情（同实例登记相册 id）再取图，于是**下载下来的单话相册
    永远是乱序图**，还被标成当前处理版本，界面连提示都没有（用户 2026-09-17
    反馈"用禁漫源图片依旧错乱"）。

    实测反证（开发机 2026-09-17，真实 CDN 原图）：
      · 1463559（相册型）原图在 12 块网格断层 13.7 → 我们公式给 12 → 倒序后连续；
      · 1469591（相册型）原图在 6 块网格断层 19.5 → 我们公式给 6 → 倒序后连续；
    所以相册 id 就是正确的哈希输入，跳过还原是错的。
    """
    ad = _adapter()
    album_id = "300000"                     # 与真实 ep 同为数字
    plain = ad.image_scramble_num(album_id, IMG + "00001.jpg")
    assert plain > 1, f"相册型也要算块数：{plain}"
    ad._album_only_seen().add(album_id)
    assert ad.image_scramble_num(album_id, IMG + "00001.jpg") == plain, \
        "登记为相册型后块数不得变成 0（那等于不还原）"
    # 合成图往返：按同一块数倒序打乱再还原，必须逐像素还原
    PIL = pytest.importorskip("PIL.Image")
    num = plain
    w, h = 20, 8 * num
    src = PIL.new("RGB", (w, h))
    px = src.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x * 10 % 256, y * 5 % 256, (x + y) % 256)
    b = io.BytesIO(); src.save(b, format="PNG"); original = b.getvalue()
    imm = PIL.open(io.BytesIO(original)).convert("RGB")
    block, rem = h // num, h % num
    scrambled = PIL.new("RGB", (w, h)); y = 0
    for i in range(num - 1, -1, -1):
        st = i * block
        en = st + block + (rem if i == num - 1 else 0)
        scrambled.paste(imm.crop((0, st, w, en)), (0, y)); y += en - st
    sb = io.BytesIO(); scrambled.save(sb, format="PNG")
    out = ad.unscramble_image(sb.getvalue(), album_id, IMG + "00001.jpg")
    got = PIL.open(io.BytesIO(out)).convert("RGB")
    assert list(got.getdata()) == list(src.getdata()), "相册型也必须还原成原图"


def test_non_numeric_chapter_id_does_not_crash():
    ad = _adapter()
    assert ad.image_scramble_num("ch-1", IMG + "00001.jpg") == 0


# ── 5) 还原失败必须抛出 ───────────────────────────────────────────────────

def test_unscramble_failure_raises_instead_of_returning_raw_bytes():
    ad = _adapter()
    with pytest.raises(MangaError):
        ad.unscramble_image(b"definitely-not-an-image", 300000, IMG + "00001.jpg")


# ── 6) 合成图往返：打乱 → 还原 → 逐像素相同 ────────────────────────────────

def test_synthetic_roundtrip_is_pixel_identical():
    PIL = pytest.importorskip("PIL.Image")
    ad = _adapter()
    ep, url = 300000, IMG + "00001.jpg"
    num = ad.image_scramble_num(ep, url)
    assert num >= 2
    w, h = 20, 8 * num            # 高度能被块数整除，避免余数带来的边界歧义
    src = PIL.new("RGB", (w, h))
    px = src.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x * 10 % 256, y * 5 % 256, (x + y) % 256)
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    original = buf.getvalue()

    # 按同一分块规则**倒序**打乱（等价于源站的混淆）
    imm = PIL.open(io.BytesIO(original)).convert("RGB")
    block = h // num
    rem = h % num
    blocks = []
    for i in range(num):
        start = i * block
        end = start + block + (0 if i != num - 1 else rem)
        blocks.append((start, end))
    scrambled = PIL.new("RGB", (w, h))
    y = 0
    for i in range(len(blocks) - 1, -1, -1):
        s, e = blocks[i]
        scrambled.paste(imm.crop((0, s, w, e)), (0, y))
        y += e - s
    sbuf = io.BytesIO()
    scrambled.save(sbuf, format="PNG")

    out = ad.unscramble_image(sbuf.getvalue(), ep, url)
    got = PIL.open(io.BytesIO(out)).convert("RGB")
    assert got.size == (w, h)
    assert list(got.getdata()) == list(src.getdata()), "还原后必须逐像素等于原图"


def test_process_version_is_declared():
    """处理版本必须存在且 ≥2：它是"只重建受影响章节"的依据"""
    assert isinstance(PROCESS_VERSION, int) and PROCESS_VERSION >= 2


# ── 7) 选择性重建缓存（只重建受影响的章节，不动用户数据）────────────────────

def test_rebuild_images_only_touches_stale_chapters(monkeypatch, tmp_path):
    import json as _json
    import app as _app
    import server.manga_api as ma
    from engine.manga.downloader import PROCESS_MARKER

    class _Ad:
        key = "jm"
        PROCESS_VERSION = 2
        def unscramble_image(self, b, ep, url):        # 有它就是"混淆源"
            return b

    monkeypatch.setattr(ma, "_manga_adapter", lambda k: _Ad())
    monkeypatch.setattr(ma, "_manga_read_adapter", lambda k: _Ad())
    monkeypatch.setattr(ma, "MANGA_CACHE_DIR", str(tmp_path / "cache"), raising=False)
    monkeypatch.setattr(ma, "MANGA_DOWNLOADS_DIR", str(tmp_path / "dl"), raising=False)
    tmp_path = tmp_path / "cache"          # 后面沿用原断言路径（读缓存那棵树）

    # 两个章节：ch-old 没标记（旧算法写的），ch-new 已标记当前版本
    old_d = tmp_path / "jm" / "comic1" / "ch-old"
    new_d = tmp_path / "jm" / "comic1" / "ch-new"
    old_d.mkdir(parents=True)
    new_d.mkdir(parents=True)
    (old_d / "0000.jpg").write_bytes(b"x" * 1200)
    (old_d / "0001.jpg").write_bytes(b"y" * 1300)
    (new_d / "0000.jpg").write_bytes(b"z" * 1400)
    (new_d / PROCESS_MARKER).write_text(
        _json.dumps({"jm": {"version": 2}}), encoding="utf-8")

    client = _app.app.test_client()
    r = client.post("/api/manga/jm/comic1/rebuild-images")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["removed_files"] == 2, body
    assert body["chapters"] == ["comic1/ch-old"], body
    assert body["kept"] == 1
    assert not (old_d / "0000.jpg").exists(), "旧处理版本的章节图必须被删（下次重拉）"
    assert (new_d / "0000.jpg").is_file(), "当前版本的章节不能被动"
    assert body["freed_bytes"] >= 2500

    # all=1：连当前版本也重建（谨慎入口）
    r2 = client.post("/api/manga/jm/comic1/rebuild-images?all=1")
    body2 = r2.get_json()
    assert body2["removed_files"] == 2, body2
    assert not (new_d / "0000.jpg").exists()

    # 下载目录同样要覆盖（读端优先用下载目录，旧算法写的下载图也必须能重建）
    dl_ch = tmp_path.parent / "dl" / "jm" / "comic1" / "ch-dl"
    dl_ch.mkdir(parents=True, exist_ok=True)
    (dl_ch / "0000.jpg").write_bytes(b"d" * 1100)
    r4 = client.post("/api/manga/jm/rebuild-images")        # 源级
    b4 = r4.get_json()
    assert b4["removed_files"] >= 1, b4
    assert not (dl_ch / "0000.jpg").exists(), "下载目录里的旧图片也要能重建"

    # 只重建指定章节（注意：上面的删除会把空的话目录一起清掉，需要重建目录）
    old_d.mkdir(parents=True, exist_ok=True)
    (old_d / "0000.jpg").write_bytes(b"q" * 1000)
    r3 = client.post("/api/manga/jm/comic1/rebuild-images?chapter=ch-old")
    assert r3.get_json()["removed_files"] == 1
    assert not (old_d / "0000.jpg").exists()
