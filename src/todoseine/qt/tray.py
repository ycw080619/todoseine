"""PySide6 应用的 Win32 系统集成：单实例互斥 + 开机自启 + 窗口定位。

全部 ctypes / winreg 直调，PySide6 依赖仅限于导入路径；
非 Windows 或调用失败时静默降级。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from typing import List, Tuple


# ---------- 单实例互斥 ----------

# 使用 Global\\ 前缀确保跨用户会话隔离；名字保持稳定便于排查
_MUTEX_NAME = "TodoSeine_MainApp_Mutex"


def is_another_running() -> bool:
    """尝试创建命名互斥量，若已存在则返回 True（表示已有实例在跑）。

    返回值：
      True  → 已有实例，当前应退出或通知前一实例
      False → 当前是第一个实例，互斥量已持有（进程退出自动释放）
    """
    if os.name != "nt":
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            return True
        # GetLastError == ERROR_ALREADY_EXISTS (183) → 有人先占了
        return kernel32.GetLastError() == 183
    except Exception:  # noqa: BLE001
        return False


# ---------- 诊断：这次是谁把我拉起来的 ----------

_TH32CS_SNAPPROCESS = 0x00000002


class _ProcessEntry32W(ctypes.Structure):
    """PROCESSENTRY32W 的前半段（到 szExeFile 为止，后面的忽略）。"""

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def launcher_brief(max_len: int = 120) -> str:
    """返回「父进程 pid:名称」，用于排查"为什么又启动了一个实例"。

    只在「已有实例在运行」这个即将退出的分支里调用，进程表枚举 + 回查父进程，
    纯 ctypes、无第三方依赖；任何一步失败都返回「未知」，绝不抛异常。
    """
    if os.name != "nt":
        return "未知"
    try:
        kernel32 = ctypes.windll.kernel32
        snap = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap:
            return "未知"
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        table: dict[int, Tuple[int, str]] = {}
        try:
            if kernel32.Process32FirstW(snap, ctypes.byref(entry)):
                while True:
                    table[int(entry.th32ProcessID)] = (
                        int(entry.th32ParentProcessID), str(entry.szExeFile))
                    if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snap)
        me = int(kernel32.GetCurrentProcessId())
        parent_pid = table.get(me, (0, ""))[0]
        parent_name = table.get(parent_pid, (0, "?"))[1] or "?"
        return f"{parent_pid}:{parent_name}"[:max_len]
    except Exception:  # noqa: BLE001 - 诊断信息拿不到就算了，不能影响退出
        return "未知"


# ---------- 开机自启（Registry Run key） ----------

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "TodoSeine"


def _exe_command() -> str:
    """返回一个可被 shell 执行的命令行，用于注册表 Run 值。

    PyInstaller onefile：sys.executable 就是 exe 本身
    开发环境：pythonw.exe -m todoseine.qt.main
    """
    if getattr(sys, "frozen", False):
        # PyInstaller onefile
        return f'"{sys.executable}"'
    # 开发模式
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pyw}" -m todoseine.qt.main'


def set_autostart(enabled: bool) -> bool:
    """启用/禁用开机自启。返回是否成功。"""
    if os.name != "nt":
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        )
        if enabled:
            winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, _exe_command())
        else:
            try:
                winreg.DeleteValue(key, _APP_NAME)
            except FileNotFoundError:
                pass  # 本来就没注册
        winreg.CloseKey(key)
        return True
    except Exception:  # noqa: BLE001
        return False


def is_autostart_enabled() -> bool:
    """查询当前是否已注册开机自启。"""
    if os.name != "nt":
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _RUN_KEY,
            0,
            winreg.KEY_READ,
        )
        try:
            winreg.QueryValueEx(key, _APP_NAME)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:  # noqa: BLE001
        return False


# ---------- QQ 窗口枚举 ----------


def enum_qq_windows() -> List[Tuple[int, str, int]]:
    """枚举桌面上所有 QQ 主窗口。

    返回 [(hwnd, title, pid), ...]，按窗口面积从大到小排序。
    过滤规则（精确 AND 匹配，避免 Trae/豆包窗口误入）：
      - ClassName == "Chrome_WidgetWin_1"
      - Title.strip() == "QQ"
    """
    if os.name != "nt":
        return []
    results: List[Tuple[int, str, int, int]] = []  # (area, hwnd, title, pid)

    user32 = ctypes.windll.user32

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
    )

    def callback(hwnd, _lparam):
        # 可见 + 标题精确 "QQ" + Chrome_WidgetWin_1
        if not user32.IsWindowVisible(hwnd):
            return True
        # Title
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        title = buf.value.strip()
        if title != "QQ":
            return True
        # ClassName
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        if cls_buf.value != "Chrome_WidgetWin_1":
            return True
        # Rect & area
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return True
        area = w * h
        # PID
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        results.append((area, int(hwnd), title, int(pid.value)))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    results.sort(key=lambda x: x[0], reverse=True)
    return [(hwnd, title, pid) for (_area, hwnd, title, pid) in results]


def get_primary_qq_hwnd() -> int:
    """取面积最大的 QQ 主窗口句柄；找不到返回 0。"""
    wins = enum_qq_windows()
    return wins[0][0] if wins else 0
