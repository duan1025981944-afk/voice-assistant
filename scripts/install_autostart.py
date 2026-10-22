"""开机自启：在"启动"文件夹里放一个快捷方式，指向 pythonw main.py（没有黑窗口）。

    python scripts/install_autostart.py            装
    python scripts/install_autostart.py --remove   卸

只动当前用户的启动文件夹（shell:startup），不需要管理员权限，也不改注册表。
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STARTUP = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
LINK = STARTUP / "语音助手.lnk"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--remove", action="store_true", help="删掉自启快捷方式")
    args = parser.parse_args()

    if args.remove:
        if LINK.exists():
            LINK.unlink()
            print(f"已删除：{LINK}")
        else:
            print("本来就没有装")
        return 0

    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        print(f"找不到 {pythonw}，先按 README 建好 .venv")
        return 1

    import win32com.client
    shortcut = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(LINK))
    shortcut.TargetPath = str(pythonw)
    shortcut.Arguments = f'"{ROOT / "main.py"}"'
    shortcut.WorkingDirectory = str(ROOT)
    shortcut.Description = "本地语音助手"
    shortcut.Save()
    print(f"已创建：{LINK}\n下次登录 Windows 时会自动启动。现在想启动就双击它。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
