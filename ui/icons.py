"""托盘和菜单用的图：她的头像，加一个表示状态的小圆点。

头像来自 assets/icon.png（scripts/make_icon.py 生成的透明底贴纸图），
裁出脸那一块——全身图缩到托盘的 16–24 像素就只剩一团了。
没有这张图时退回到纯色圆形图标，程序照样能跑。

状态 → 圆点
    待命   不加点，就是头像
    在听   绿    在想   蓝    在说   橙
    暂停   头像变灰 + 红点
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from assistant import State
from config.loader import PROJECT_ROOT
from voice.paths import DATA_DIR

AVATAR = PROJECT_ROOT / "assets" / "icon.png"
ICON_DIR = DATA_DIR / "icons"

# 头像裁剪：从人物顶端往下 6% 开始，取人物高度 58% 的正方形，水平居中
HEAD_TOP = 0.06
HEAD_SIZE = 0.58

DOT_COLORS = {
    State.LISTENING: (39, 174, 96),
    State.THINKING: (41, 128, 185),
    State.SPEAKING: (230, 126, 34),
    State.PAUSED: (220, 53, 69),
}
FALLBACK_COLOR = (140, 140, 140)
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64]


def head() -> Image.Image:
    """头像（RGBA 正方形）。"""
    if not AVATAR.exists():
        img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((8, 8, 248, 248), fill=FALLBACK_COLOR)
        return img
    img = Image.open(AVATAR).convert("RGBA")
    left, top, right, bottom = img.getbbox()
    height = bottom - top
    side = int(height * HEAD_SIZE)
    x0 = (left + right) // 2 - side // 2
    y0 = top + int(height * HEAD_TOP)
    return img.crop((x0, y0, x0 + side, y0 + side)).resize((256, 256), Image.LANCZOS)


def state_image(face: Image.Image, state: State) -> Image.Image:
    """256×256 的托盘图：头像 + 右下角圆点（带白圈，在深色任务栏上也看得清）。"""
    img = face.copy()
    if state is State.PAUSED:
        gray = ImageOps.grayscale(img).convert("RGBA")
        gray.putalpha(img.getchannel("A"))
        img = gray
    color = DOT_COLORS.get(state)
    if color:
        draw = ImageDraw.Draw(img)
        d, ring = 112, 14                     # 画大再缩小，边缘才平滑
        box = (256 - d, 256 - d, 255, 255)
        draw.ellipse(box, fill="white")
        draw.ellipse((box[0] + ring, box[1] + ring, box[2] - ring, box[3] - ring), fill=color)
    return img


def write_state_icons() -> dict[State, Path]:
    """每个状态生成一个多尺寸 .ico，放在 data/icons/。启动时调一次，几十毫秒。"""
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    face = head()
    paths = {}
    for state in State:
        path = ICON_DIR / f"{state.name.lower()}.ico"
        state_image(face, state).save(path, sizes=[(s, s) for s in ICO_SIZES])
        paths[state] = path
    return paths


def round_avatar(size: int) -> Image.Image:
    """菜单顶部用的圆形头像。"""
    face = head().resize((size * 4, size * 4), Image.LANCZOS)
    mask = Image.new("L", face.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, face.width - 1, face.height - 1), fill=255)
    face.putalpha(Image.composite(face.getchannel("A"), mask, mask))
    return face.resize((size, size), Image.LANCZOS)
