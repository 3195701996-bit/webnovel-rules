# -*- coding: utf-8 -*-
"""0.66.0 回归（离线）：禁漫兜底路径的**落盘格式与 Content-Type**。

背景（2026-09-17 用户拍板 + 实测）：
  · 源图本身就是**有损 WebP**（142–190KB/张）；APK 里的 Pillow 没有 WebP 支持，
    所以走 Android 解码兜底。旧实现把结果编码成 **PNG**（~2MB/张）——
    体积大 10 倍、编码更慢，**画质却不会因此变成无损**（源图已经有损）。
  · 改为 Android 侧编码 **JPEG q95** 作为中间格式，再由 Pillow 按块倒序置换后
    以 **JPEG q92** 落盘（`_permute_blocks_pillow` 保留输入格式的行为）。
  · 随之而来的一个老问题要一起修：这些字节被写进以源扩展名命名的文件
    （历史先是 PNG 字节叫 `.webp`，现在是 JPEG 字节叫 `.webp`），
    而静态直出原来照**扩展名**回 Content-Type（image/webp）——与实际内容不符，
    客户端只是靠嗅探侥幸读对。现在按**文件头**判断。

本用例锁住：
  1. 兜底用的是 JPEG 而不是 PNG（函数名与质量常量都明确）；
  2. 置换后的字节确实是 JPEG（FFD8FF 开头），且**体积不再膨胀**；
  3. `_sniff_image_mime()` 对 JPEG/PNG/WebP/GIF/AVIF 与"非图片"的判断；
  4. 静态直出用的是嗅探结果（源码级断言：`_serve_local_image` 里不能再写死
     `mimetype="image/" + 扩展名`）。
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.manga.jm as jm  # noqa: E402
import server.manga_api as mapi  # noqa: E402


def test_android_fallback_encodes_jpeg_not_png():
    assert jm._ANDROID_INTERMEDIATE_QUALITY == 95, "中间质量应为 95（给最终 q92 留余量）"
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "engine", "manga", "jm.py"), encoding="utf-8").read()
    assert "Bitmap.CompressFormat.JPEG" in src, "兜底应编码 JPEG"
    assert "Bitmap.CompressFormat.PNG" not in src, "不应再用 PNG 兜底（体积大 10 倍且无画质收益）"
    # 旧名字保留为别名，避免外部脚本/历史调用点炸掉
    assert jm._decode_to_png_via_android is jm._decode_to_jpeg_via_android


def test_permuted_jpeg_stays_jpeg_and_does_not_bloat():
    """置换后仍是 JPEG，体积与源图同量级（旧 PNG 路径会膨胀约 10 倍）。"""
    PIL = pytest.importorskip("PIL.Image")
    # 造一张 200x2000 的"漫画"图：源站是**有损**格式，这里用 JPEG 模拟
    src = PIL.new("RGB", (200, 2000))
    px = src.load()
    for y in range(2000):
        for x in range(200):
            px[x, y] = (x * 7 % 256, y * 3 % 256, (x + y) % 256)
    b = io.BytesIO(); src.save(b, format="JPEG", quality=95)
    raw = b.getvalue()

    out = jm._permute_blocks_pillow(raw, 10)
    assert out[:3] == b"\xff\xd8\xff", "输出必须是 JPEG"
    ratio = len(out) / len(raw)
    assert ratio < 2.0, f"体积不应膨胀（源 {len(raw)}B → 结果 {len(out)}B，比值 {ratio:.2f}）"
    got = PIL.open(io.BytesIO(out))
    assert got.format == "JPEG" and got.size == (200, 2000)


def test_sniff_image_mime(tmp_path):
    cases = {
        "a.webp": (b"\xff\xd8\xff\xe0" + b"x" * 40, "image/jpeg"),      # JPEG 字节、.webp 名字
        "b.webp": (b"\x89PNG\r\n\x1a\n" + b"x" * 40, "image/png"),      # PNG 字节、.webp 名字
        "c.webp": (b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"x" * 20, "image/webp"),
        "d.bin": (b"GIF89a" + b"x" * 40, "image/gif"),
        "e.bin": (b"\x00\x00\x00\x20ftypavif" + b"x" * 20, "image/avif"),
        "f.txt": (b"not an image at all", ""),
    }
    for name, (data, want) in cases.items():
        p = tmp_path / name
        p.write_bytes(data)
        assert mapi._sniff_image_mime(str(p)) == want, name
    # 文件不存在不能抛
    assert mapi._sniff_image_mime(str(tmp_path / "missing.webp")) == ""


def test_local_serve_uses_sniffed_mime():
    """源码级：静态直出不得再按扩展名写死 Content-Type。"""
    import inspect
    src = inspect.getsource(mapi._serve_local_image)
    assert "_sniff_image_mime(" in src, "直出必须按内容嗅探 mimetype"
    assert 'mimetype="image/" + _ext.lstrip(".")' not in src, "不得按扩展名写死 mimetype"
