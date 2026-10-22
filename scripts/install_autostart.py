"""创建"语音助手"快捷方式，指向 pythonw main.py（没有黑窗口），图标用 assets/icon.ico。

    python scripts/install_autostart.py                     开机自启（放进"启动"文件夹）
    python scripts/install_autostart.py --desktop           桌面快捷方式，双击手动启动
    python scripts/install_autostart.py --remove            删掉开机自启
    python scripts/install_autostart.py --desktop --remove  删掉桌面快捷方式

只在当前用户的"启动"文件夹 / 桌面上放快捷方式，不需要管理员权限，也不改注册表。
已经在运行时再双击，会弹窗提示"已经有一个在运行了"，不会开第二个。
换图标：先运行 scripts/make_icon.py，再重新运行这个脚本。
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "assets" / "icon.ico"
LINK_NAME = "语音助手.lnk"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--desktop", action="store_true", help="放到桌面，而不是开机自启")
    parser.add_argument("--remove", action="store_true", help="删掉快捷方式")
    args = parser.parse_args()

    import win32com.client
    shell = win32com.client.Dispatch("WScript.Shell")
    # 用系统给的路径，桌面被 OneDrive 接管、或者改过位置也能找对
    folder = Path(shell.SpecialFolders("Desktop" if args.desktop else "Startup"))
    link = folder / LINK_NAME

    if args.remove:
        if link.exists():
            link.unlink()
            print(f"已删除：{link}")
        else:
            print(f"本来就没有：{link}")
        return 0

    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        print(f"找不到 {pythonw}，先按 README 建好 .venv")
        return 1

    shortcut = shell.CreateShortcut(str(link))
    shortcut.TargetPath = str(pythonw)
    shortcut.Arguments = f'"{ROOT / "main.py"}"'
    shortcut.WorkingDirectory = str(ROOT)
    shortcut.Description = "本地语音助手"
    if ICON.exists():
        shortcut.IconLocation = f"{ICON},0"
    shortcut.Save()

    print(f"已创建：{link}")
    if args.desktop:
        print("双击桌面上的「语音助手」就能启动，托盘里会出现图标。")
    else:
        print("下次登录 Windows 时会自动启动。现在想启动就双击它。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
