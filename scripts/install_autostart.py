"""登录自启安装 / 卸载（P1）。

「调用即记录」以**服务在运行**为前提：进程内的 15s 增量轮询不会跨进程工作。
这个脚本把看板登记到当前用户的登录项（``HKCU\\...\\Run``），开机即采，
不需要管理员权限，也不动系统目录。

设计取舍
--------
* 只写 **HKCU**（当前用户），不写 HKLM——不需要管理员，也不会影响其他用户。
* 启动命令走 ``scripts/autostart_launcher.py``（见其文档字符串：为什么不能
  直接写 uvicorn 命令）。优先 ``pythonw.exe``（无控制台窗口），找不到退回
  ``python.exe``。
* ``--dry-run`` 只打印将要写入的内容，不碰注册表——和项目里其他工具一样，
  「看一眼」和「真做」分开。

用法::

    python scripts/install_autostart.py --dry-run    # 看会写什么
    python scripts/install_autostart.py --install    # 安装（幂等）
    python scripts/install_autostart.py --status     # 查当前状态
    python scripts/install_autostart.py --uninstall  # 卸载（没有也不算错）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = PROJECT_ROOT / "scripts" / "autostart_launcher.py"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "TokenUsageMetering"


def _python_command() -> list[str]:
    """优先 pythonw.exe（无窗口），退回当前解释器。"""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    chosen = pythonw if pythonw.exists() else exe
    return [str(chosen), str(LAUNCHER)]


def build_command() -> str:
    """Run 键的命令行值（带引号，路径含空格也不会散架）。"""
    parts = _python_command()
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


def _open_key(*, writable: bool):
    import winreg

    return winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        RUN_KEY_PATH,
        0,
        winreg.KEY_READ | (winreg.KEY_WRITE if writable else 0),
    )


def read_current() -> str | None:
    """读当前登记值；未安装返回 None。"""
    import winreg

    try:
        with _open_key(writable=False) as key:
            value, _kind = winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError as exc:  # 注册表不可达（极少见）：如实说，不假装没装
        print(f"!! 读注册表失败：{exc}")
        return None


def install(dry_run: bool = False) -> int:
    if not LAUNCHER.exists():
        print(f"!! 找不到启动器：{LAUNCHER}")
        return 1
    command = build_command()
    print(f"将写入 HKCU\\{RUN_KEY_PATH}")
    print(f"  值名   : {RUN_VALUE_NAME}")
    print(f"  命令行 : {command}")
    if dry_run:
        print("（dry-run：未写入）")
        return 0

    import winreg

    with _open_key(writable=True) as key:
        winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, command)
    print("已安装。下次登录时看板会自动启动（127.0.0.1:8787）。")
    print("验证：登录后打开 http://127.0.0.1:8787/ ，或看 data/autostart.log。")
    return 0


def uninstall(dry_run: bool = False) -> int:
    import winreg

    current = read_current()
    if current is None:
        print("未安装（无需卸载）。")
        return 0
    if dry_run:
        print(f"（dry-run：将删除 {RUN_VALUE_NAME} = {current}）")
        return 0
    try:
        with _open_key(writable=True) as key:
            winreg.DeleteValue(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        print("未安装（无需卸载）。")
        return 0
    print(f"已卸载（删除的是「登录自启」登记；数据与代码不受影响）。")
    return 0


def status() -> int:
    current = read_current()
    if current is None:
        print(f"未安装。安装：python scripts/install_autostart.py --install")
        return 1
    print(f"已安装：HKCU\\{RUN_KEY_PATH}\\{RUN_VALUE_NAME}")
    print(f"  {current}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="看板登录自启（HKCU Run 键）")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--install", action="store_true", help="安装登录自启（幂等）")
    group.add_argument("--uninstall", action="store_true", help="卸载登录自启")
    group.add_argument("--status", action="store_true", help="查看当前登记状态")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要做的事，不写注册表")
    args = ap.parse_args()

    if args.install:
        return install(dry_run=args.dry_run)
    if args.uninstall:
        return uninstall(dry_run=args.dry_run)
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
