"""PySide6 窗口的 DWM / Acrylic / Mica 系统材质工具。

所有 ctypes 直调，零第三方依赖；非 Windows 或调用失败时静默降级。
与 dashboard/win32.py 并行存在——但目标不同：
  - win32.py 服务于 Tkinter root.winfo_id()
  - dwm.py  服务于 PySide6 QWidget.winId() -> int(hwnd)
"""

from __future__ import annotations

import ctypes
import os
from typing import Optional


# ---------- ctypes 结构体 ----------

class Margins(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


class AccentPolicy(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_uint32),
        ("AccentFlags", ctypes.c_uint32),
        ("GradientColor", ctypes.c_uint32),
        ("AnimationId", ctypes.c_uint32),
    ]


class WinCompAttrData(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_uint32),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_size_t),
    ]


# ---------- DWM 属性常量 ----------

# DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_DARK_MODE = 20
# DWMWA_WINDOW_CORNER_PREFERENCE = 33 (DWMWCP_ROUND=2)
_DWMWA_CORNER = 33
# DWMWA_SYSTEMBACKDROP_TYPE = 38 (2=Mica, 3=Mica Alt, 4=Tabbed)
_DWMWA_BACKDROP = 38
# DWMWA_NCRENDERING_POLICY = 2
_DWMWA_NCRENDERING = 2


# ---------- 工具函数 ----------

def _dwmapi() -> Optional[ctypes.WinDLL]:
    """懒加载 dwmapi.dll。"""
    if os.name != "nt":
        return None
    try:
        return ctypes.windll.dwmapi
    except OSError:
        return None


def _user32() -> Optional[ctypes.WinDLL]:
    if os.name != "nt":
        return None
    try:
        return ctypes.windll.user32
    except OSError:
        return None


def extend_frame(hwnd: int) -> bool:
    """把 DWM 玻璃帧扩展到整个客户区——系统材质/Acrylic 的渲染面。"""
    dll = _dwmapi()
    if not dll or not hwnd:
        return False
    m = Margins(-1, -1, -1, -1)
    r = dll.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(m))
    return r == 0  # S_OK


def set_round_corners(hwnd: int) -> bool:
    """Win11 原生圆角（DWMWCP_ROUND=2）。旧系统静默忽略。"""
    dll = _dwmapi()
    if not dll or not hwnd:
        return False
    val = ctypes.c_int(2)
    r = dll.DwmSetWindowAttribute(
        hwnd, _DWMWA_CORNER, ctypes.byref(val), ctypes.sizeof(val)
    )
    return r == 0


def set_immersive_dark(hwnd: int, dark: bool = True) -> bool:
    """沉浸式深色模式。"""
    dll = _dwmapi()
    if not dll or not hwnd:
        return False
    val = ctypes.c_int(1 if dark else 0)
    r = dll.DwmSetWindowAttribute(
        hwnd, _DWMWA_DARK_MODE, ctypes.byref(val), ctypes.sizeof(val)
    )
    return r == 0


def set_system_backdrop(hwnd: int, kind: int = 3) -> bool:
    """Win11 22H2+ 系统材质。kind: 2=Mica, 3=MicaAlt, 4=Tabbed。

    只在客户区透明的窗口上可见；系统不支持返回 False。
    """
    dll = _dwmapi()
    if not dll or not hwnd:
        return False
    extend_frame(hwnd)
    val = ctypes.c_int(kind)
    r = dll.DwmSetWindowAttribute(
        hwnd, _DWMWA_BACKDROP, ctypes.byref(val), ctypes.sizeof(val)
    )
    return r == 0


def set_acrylic(hwnd: int, tint_bgr: int = 0x80292929) -> bool:
    """Acrylic 亚克力材质（属性 30 + AccentState=3）。

    tint_bgr 是 0xAABBGGRR，高位字节控制 luminosity 开关（0x80xxxxx 开启）。
    前置条件：窗口背景涂纯黑 + extend_frame 已调用。
    """
    user32 = _user32()
    if not user32 or not hwnd:
        return False
    # 先开深色模式
    set_immersive_dark(hwnd, True)
    policy = AccentPolicy(3, 0, tint_bgr, 0)
    data = WinCompAttrData(30, ctypes.addressof(policy), ctypes.sizeof(policy))
    ok = bool(user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data)))
    extend_frame(hwnd)
    return ok


def set_blur_behind(hwnd: int) -> bool:
    """Win10+ 毛玻璃（AccentState=3 + 属性 19 BLURBEHIND）。"""
    user32 = _user32()
    if not user32 or not hwnd:
        return False
    policy = AccentPolicy(3, 0, 0x00101010, 0)
    data = WinCompAttrData(19, ctypes.addressof(policy), ctypes.sizeof(policy))
    return bool(user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data)))


# ---------- 截屏排除（隐私） ----------

# WDA_NONE = 0, WDA_MONITOR = 1, WDA_EXCLUDEFROMCAPTURE = 0x11
_WDA_EXCLUDEFROMCAPTURE = 0x00000011


def set_exclude_from_capture(hwnd: int, exclude: bool = True) -> bool:
    """让窗口不出现在截屏 / 录屏 / 远程桌面里（Win10 2004+）。

    看板含敏感待办时开启，截图工具只能看到一块黑。
    旧系统调用失败返回 False，不影响正常使用。
    """
    user32 = _user32()
    if not user32 or not hwnd:
        return False
    affinity = _WDA_EXCLUDEFROMCAPTURE if exclude else 0
    return bool(user32.SetWindowDisplayAffinity(hwnd, affinity))


# ---------- DWM 阴影 ----------

# DWMWA_NCRENDERING_POLICY = 2, DWMNCRP_ENABLED = 2
_DWMNCRP_ENABLED = 2


def enable_shadow(hwnd: int, enable: bool = True) -> bool:
    """启用 / 禁用 DWM 非客户区渲染（无边框窗口的系统阴影）。

    无边框窗口默认没有阴影，开启后系统会在窗口四周画投影。
    配合 WM_NCCALCSIZE 保留 1px 非客户区才能看到效果。
    """
    dll = _dwmapi()
    if not dll or not hwnd:
        return False
    val = ctypes.c_int(_DWMNCRP_ENABLED if enable else 0)
    r = dll.DwmSetWindowAttribute(
        hwnd, _DWMWA_NCRENDERING, ctypes.byref(val), ctypes.sizeof(val)
    )
    return r == 0


# ---------- 窗口扩展样式 ----------

_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000


def set_no_activate(hwnd: int, enabled: bool = True) -> bool:
    """设置 / 清除 WS_EX_NOACTIVATE：窗口被点击时不抢前台焦点。

    看板要求点击、拖拽、勾选都不激活窗口，否则会把用户正在打字的
    前台窗口顶到后面去。需要输入文字时由调用方临时清除。
    """
    user32 = _user32()
    if not user32 or not hwnd:
        return False
    try:
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            getter = user32.GetWindowLongPtrW
            setter = user32.SetWindowLongPtrW
            getter.restype = ctypes.c_longlong
            getter.argtypes = [ctypes.c_void_p, ctypes.c_int]
            setter.restype = ctypes.c_longlong
            setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_longlong]
        else:
            getter = user32.GetWindowLongW
            setter = user32.SetWindowLongW
        style = int(getter(hwnd, _GWL_EXSTYLE))
    except Exception:  # noqa: BLE001 - 极老的系统没有 *Ptr 版本
        return False
    new_style = (style | _WS_EX_NOACTIVATE) if enabled else (style & ~_WS_EX_NOACTIVATE)
    if new_style != style:
        setter(hwnd, _GWL_EXSTYLE, new_style)
    return True


# ---------- 辅助：从 QWidget 拿 hwnd ----------

def hwnd_of(widget) -> int:
    """从 PySide6 QWidget 拿到 Win32 HWND（int）。"""
    try:
        return int(widget.winId())
    except Exception:  # noqa: BLE001
        return 0
