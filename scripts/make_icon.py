"""把一张图片做成快捷方式用的 .ico 图标。

    python scripts/make_icon.py                       用 assets/avatar.jpg
    python scripts/make_icon.py 别的图片.png          换一张图

白底图片会自动抠掉外面的白色背景，留一圈白边，像贴纸一样；
同时输出 assets/icon.ico（多种尺寸）和 assets/icon.png（预览用）。
换了图之后再运行一次 scripts/install_autostart.py（加不加 --desktop 都一样），快捷方式就会用新图标。
"""
import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SIZES = [16, 24, 32, 48, 64, 128, 256]
BACKGROUND_TOLERANCE = 40     # 和纯白差多少以内算背景（JPG 的白底不是纯白）
STICKER_BORDER = 0.012        # 白边宽度，占图片边长的比例


def cut_out(img: Image.Image) -> Image.Image:
    """从四个角往里灌水，灌到的近白色区域就是背景，变成透明；再给人物留一圈白边。"""
    rgb = img.convert("RGB")
    marker = (255, 0, 255)
    w, h = rgb.size
    for corner in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        if rgb.getpixel(corner) != marker:
            ImageDraw.floodfill(rgb, corner, marker, thresh=BACKGROUND_TOLERANCE)

    is_background = (np.asarray(rgb) == marker).all(axis=2)
    subject = Image.fromarray(np.where(is_background, 0, 255).astype(np.uint8))
    border = max(3, int(min(w, h) * STICKER_BORDER)) * 2 + 1
    alpha = subject.filter(ImageFilter.MaxFilter(border)).filter(ImageFilter.GaussianBlur(1.2))

    out = img.convert("RGBA")
    white = Image.new("RGBA", img.size, (255, 255, 255, 255))
    out = Image.composite(out, white, subject)       # 白边那一圈用纯白（JPG 的白底有杂色）
    out.putalpha(alpha)
    return out


def square(img: Image.Image) -> Image.Image:
    """裁到人物边框，再补成正方形（透明），图标才不会被拉扁。"""
    img = img.crop(img.getbbox())
    side = max(img.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
    return canvas


def main() -> int:
    parser = argparse.ArgumentParser(description="图片 → 快捷方式图标")
    parser.add_argument("image", nargs="?", default=str(ASSETS / "avatar.jpg"))
    args = parser.parse_args()

    icon = square(cut_out(Image.open(args.image)))
    big = icon.resize((256, 256), Image.LANCZOS)
    big.save(ASSETS / "icon.png")
    big.save(ASSETS / "icon.ico", sizes=[(s, s) for s in SIZES])
    print(f"已生成 {ASSETS / 'icon.ico'}（预览：icon.png）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
