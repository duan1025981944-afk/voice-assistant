"""托盘图标的生成。菜单的样子要靠眼睛看（截图核对过），这里只测不需要屏幕的部分。"""
from PIL import Image

from assistant import State
from ui import icons


def test_state_icons_written():
    paths = icons.write_state_icons()
    assert set(paths) == set(State)
    for path in paths.values():
        sizes = Image.open(path).info["sizes"]
        assert (16, 16) in sizes and (64, 64) in sizes


def test_paused_icon_is_gray_with_dot():
    face = icons.head()
    paused = icons.state_image(face, State.PAUSED).convert("RGB")
    r, g, b = paused.getpixel((128, 100))           # 脸部中间：灰度图三个通道相等
    assert r == g == b
    assert paused.getpixel((255 - 56, 255 - 56)) == icons.DOT_COLORS[State.PAUSED]   # 右下角圆点


def test_idle_icon_has_no_dot():
    face = icons.head()
    assert icons.state_image(face, State.IDLE).tobytes() == face.tobytes()
