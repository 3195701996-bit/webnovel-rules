# -*- coding: utf-8 -*-
"""生成启动图标（本脚本可重复执行，产物提交进版本库）。

图标语义：深色底 + 展开的书页 + 书签丝带，面向读者，不再使用"服务器"意象。
同时输出传统 PNG 图标（API 24/25 回退）与自适应图标前景/背景（API 26+）。

用法：venv/bin/python android/tools/make_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

RES = Path(__file__).resolve().parent.parent / "app/src/main/res"

BG_TOP = (35, 42, 56)
BG_BOTTOM = (18, 23, 32)
PAGE = (242, 245, 250)
PAGE_SHADE = (198, 208, 222)
SPINE = (120, 133, 152)
RIBBON = (232, 163, 61)

# 传统图标的密度与边长（px）
LEGACY = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
# 自适应图标画布 108dp
ADAPTIVE = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}


def _background(size: int) -> Image.Image:
    """竖直渐变底，自适应图标要求铺满整块画布。"""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        t = y / max(size - 1, 1)
        row = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
        for x in range(size):
            px[x, y] = row
    return img


def _draw_book(draw: ImageDraw.ImageDraw, cx: float, cy: float, scale: float,
               background: bool) -> None:
    """在 (cx, cy) 处以 scale 为整体宽度绘制一本摊开的书。"""
    w = scale                      # 书总宽
    h = scale * 0.74               # 书总高
    left = cx - w / 2
    top = cy - h / 2
    gutter = w * 0.035             # 书脊缝隙
    curl = h * 0.10                # 页面上缘的外翻弧度

    # 左右两页：上缘外侧略低，形成翻开的弧线
    for side in (-1, 1):
        outer = left if side < 0 else left + w
        inner = cx - gutter / 2 if side < 0 else cx + gutter / 2
        poly = [
            (outer, top + curl),
            (outer + side * w * 0.06, top),          # 上缘抬起
            (inner, top + curl * 0.35),              # 靠近书脊处最低
            (inner, top + h),
            (outer, top + h - curl * 0.6),
        ]
        draw.polygon(poly, fill=PAGE)

    # 页面下缘阴影，避免纯平色块
    for side in (-1, 1):
        outer = left if side < 0 else left + w
        inner = cx - gutter / 2 if side < 0 else cx + gutter / 2
        draw.polygon([(inner, top + h * 0.82), (inner, top + h),
                      (outer, top + h - curl * 0.6),
                      (outer, top + h - curl * 0.6 - h * 0.10)], fill=PAGE_SHADE)

    # 书脊
    draw.polygon([(cx - gutter / 2, top + curl * 0.35),
                  (cx + gutter / 2, top + curl * 0.35),
                  (cx + gutter / 2, top + h), (cx - gutter / 2, top + h)], fill=SPINE)

    # 书签丝带：从右页上缘垂下
    rw = w * 0.09
    rx = cx + w * 0.20
    ry = top + curl * 0.5
    draw.polygon([(rx, ry), (rx + rw, ry), (rx + rw, ry + h * 0.62),
                  (rx + rw / 2, ry + h * 0.50), (rx, ry + h * 0.62)], fill=RIBBON)


def legacy_icon(size: int) -> Image.Image:
    """传统图标：满幅圆形底 + 书。"""
    ss = size * 4                                   # 先超采样再缩小，边缘更干净
    base = _background(ss).convert("RGBA")
    mask = Image.new("L", (ss, ss), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, ss - 1, ss - 1], fill=255)
    base.putalpha(mask)
    _draw_book(ImageDraw.Draw(base), ss / 2, ss / 2 + ss * 0.015, ss * 0.60, background=False)
    return base.resize((size, size), Image.LANCZOS)


def round_icon(size: int) -> Image.Image:
    """圆形图标：自适应启动器在圆形遮罩下使用。"""
    return legacy_icon(size)


def adaptive_foreground(size: int) -> Image.Image:
    """自适应图标前景：透明底，书图案收敛在中间 66dp 安全区内。"""
    ss = size * 2
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    _draw_book(ImageDraw.Draw(img), ss / 2, ss / 2, ss * 0.46, background=True)
    return img.resize((size, size), Image.LANCZOS)


def adaptive_background(size: int) -> Image.Image:
    return _background(size).convert("RGBA")


ADAPTIVE_XML = """<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@mipmap/ic_launcher_background" />
    <foreground android:drawable="@mipmap/ic_launcher_foreground" />
    <monochrome android:drawable="@mipmap/ic_launcher_foreground" />
</adaptive-icon>
"""


def main() -> None:
    for density, px in LEGACY.items():
        d = RES / f"mipmap-{density}"
        d.mkdir(parents=True, exist_ok=True)
        legacy_icon(px).save(d / "ic_launcher.png")
        round_icon(px).save(d / "ic_launcher_round.png")
        print(f"mipmap-{density}/ic_launcher.png {px}x{px}")

    for density, px in ADAPTIVE.items():
        d = RES / f"mipmap-{density}"
        d.mkdir(parents=True, exist_ok=True)
        adaptive_background(px).save(d / "ic_launcher_background.png")
        adaptive_foreground(px).save(d / "ic_launcher_foreground.png")
        print(f"mipmap-{density}/ic_launcher_foreground.png {px}x{px}")

    for name in ("ic_launcher.xml", "ic_launcher_round.xml"):
        d = RES / "mipmap-anydpi-v26"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(ADAPTIVE_XML, encoding="utf-8")
        print(f"mipmap-anydpi-v26/{name}")


if __name__ == "__main__":
    main()
