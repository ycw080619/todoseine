"""桌面图标入口：看板没开就开看板；看板已开就打开控制台（设置）。

对应需求：双击桌面图标 → 先出待办窗口；窗口已开时再双击 → 打开设置。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASH_MUTEX = "TodoSeine_MainApp_Mutex"   # 与 qt/tray.py 的 _MUTEX_NAME 同名
CONSOLE_TITLE = "TodoSeine 控制台"

SYNCHRONIZE = 0x00100000


def dashboard_running() -> bool:
    """只看不建：OpenMutex 不会创建互斥量，所以不会挡住看板自己的单实例检查。"""
    try:
        h = ctypes.windll.kernel32.OpenMutexW(SYNCHRONIZE, False, DASH_MUTEX)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def console_window() -> int:
    """找已经打开的控制台窗口，返回句柄；没有返回 0。"""
    found = []
    user32 = ctypes.windll.user32

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if CONSOLE_TITLE in buf.value and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else 0


def env() -> dict:
    e = dict(os.environ)
    e["PYTHONPATH"] = str(ROOT / "src")
    e.setdefault("PYTHONIOENCODING", "utf-8")
    return e


def main() -> int:
    py = sys.executable

    if dashboard_running():
        hwnd = console_window()
        if hwnd:
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return 0
        target = ROOT / "tools" / "qq_db_export" / "console.py"
        subprocess.Popen([py, str(target), "--page", "general"], cwd=str(ROOT), env=env())
        return 0

    # Qt 版 dash 不接受 --db/--interval：DB 路径从配置读（config.load 含旧路径兜底）
    args = [py, "-m", "todoseine", "dash"]
    subprocess.Popen(args, cwd=str(ROOT), env=env())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
