"""看板的 Win32 交互：单实例互斥、全局热键、窗口层级、毛玻璃与系统材质。

全部 ctypes 直调，零第三方依赖；非 Windows 或调用失败时静默降级。
"""

from __future__ import annotations

import os
import threading

def _single_instance(name: str = "Chat2TodoDashboard") -> bool:
    """用命名互斥量做单实例，避免开机自启重复拉起。"""
    try:
        import ctypes

        ctypes.windll.kernel32.CreateMutexW(None, False, name)
        already = ctypes.windll.kernel32.GetLastError() == 183   # ERROR_ALREADY_EXISTS
        return not already
    except Exception:  # noqa: BLE001
        return True


def _start_hotkey(callback, mods: int = 0x0003 | 0x4000, vk: int = 0x44) -> None:
    """注册全局热键 Ctrl+Alt+D（MOD_ALT|MOD_CONTROL|MOD_NOREPEAT），后台线程收消息。"""
    def loop() -> None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            if not user32.RegisterHotKey(None, 1, mods, vk):
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == 0x0312:      # WM_HOTKEY
                    callback()
        except Exception:  # noqa: BLE001
            return

    threading.Thread(target=loop, daemon=True).start()


def _hwnd_of(root) -> int:
    """拿到顶层窗口句柄（overrideredirect 下 winfo_id 可能是子窗口）。"""
    try:
        import ctypes

        wid = root.winfo_id()
        user32 = ctypes.windll.user32
        anc = user32.GetAncestor(wid, 2)      # GA_ROOT：取真正的顶层窗口
        return anc or wid
    except Exception:  # noqa: BLE001
        return 0


def _set_layer(root, keep_topmost: bool) -> None:
    """贴桌面层 / 置顶二选一。

    keep_topmost=False 时把窗口压到 Z 序最底（HWND_BOTTOM），
    效果是「贴在桌面上、不挡别的窗口」；True 则常驻最上层。
    """
    try:
        import ctypes

        hwnd = _hwnd_of(root)
        if not hwnd:
            return
        root.attributes("-topmost", keep_topmost)
        hwnd_insert_after = -1 if keep_topmost else 1   # HWND_TOPMOST / HWND_BOTTOM
        flags = 0x0001 | 0x0002 | 0x0010                # NOSIZE|NOMOVE|NOACTIVATE
        ctypes.windll.user32.SetWindowPos(hwnd, hwnd_insert_after, 0, 0, 0, 0, flags)
    except Exception:  # noqa: BLE001
        pass


def _apply_blur(root, enable: bool) -> bool:
    """Win10+ 毛玻璃（SetWindowCompositionAttribute / ACCENT_ENABLE_BLURBEHIND）。

    与 -alpha 半透明叠加时，窗口背后的内容会糊成一片（DeskBox 的毛玻璃
    卡片感）。返回系统是否接受了设置；失败就静默退回普通半透明。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        class AccentPolicy(ctypes.Structure):
            _fields_ = [("AccentState", ctypes.c_int),
                        ("AccentFlags", ctypes.c_int),
                        ("GradientColor", ctypes.c_uint32),
                        ("AnimationId", ctypes.c_int)]

        class WinCompAttrData(ctypes.Structure):
            _fields_ = [("Attribute", ctypes.c_int),
                        ("Data", ctypes.c_void_p),
                        ("SizeOfData", ctypes.c_size_t)]

        hwnd = _hwnd_of(root)
        if not hwnd:
            return False
        # AccentState: 0=关闭 3=BLURBEHIND；GradientColor 是 ABGR 叠加色
        policy = AccentPolicy(3 if enable else 0, 0, 0x00101010, 0)
        data = WinCompAttrData(19, ctypes.addressof(policy),
                               ctypes.sizeof(policy))
        return bool(ctypes.windll.user32.SetWindowCompositionAttribute(
            hwnd, ctypes.byref(data)))
    except Exception:  # noqa: BLE001
        return False


# DWMWA_WINDOW_CORNER_PREFERENCE：Win11 22000+ 的原生圆角（DWMWCP_ROUND=2）
_DWMWA_CORNER = 33
# DWMWA_SYSTEMBACKDROP_TYPE：Win11 22H2+ 系统材质（2=Mica 3=MicaAlt 4=Tabbed）
_DWMWA_BACKDROP = 38


def _set_round_corners(root) -> bool:
    """让 DWM 给无边框窗口画原生圆角，替代 canvas 手绘圆角的锯齿。

    仅 Win11 生效；旧系统调用返回非 0，静默忽略（退回手绘圆角）。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        hwnd = _hwnd_of(root)
        if not hwnd:
            return False
        val = ctypes.c_int(2)          # DWMWCP_ROUND
        r = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, _DWMWA_CORNER, ctypes.byref(val), ctypes.sizeof(val))
        return r == 0                  # S_OK
    except Exception:  # noqa: BLE001
        return False


def _set_system_backdrop(root, kind: int = 3) -> bool:
    """Win11 22H2+ 系统材质。kind: 2=Mica、3=Mica Alt（小窗推荐）、4=Tabbed。

    只在客户区透明的窗口上可见（依赖 -transparentcolor 抠洞）；
    系统不支持时返回 False，调用方应退回 Acrylic 兜底。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        hwnd = _hwnd_of(root)
        if not hwnd:
            return False
        _extend_frame(hwnd)            # 材质渲染面，否则客户区看不到 Mica
        val = ctypes.c_int(kind)
        r = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, _DWMWA_BACKDROP, ctypes.byref(val), ctypes.sizeof(val))
        return r == 0
    except Exception:  # noqa: BLE001
        return False


def _extend_frame(hwnd) -> None:
    """把 DWM 玻璃帧扩展到整个客户区——系统材质/Acrylic 的渲染面。

    只做 SetWindowCompositionAttribute 而不扩帧，材质无处附着（窗口会
    变成纯透明洞）。-1 表示四边全部延伸进来。
    """
    import ctypes

    class Margins(ctypes.Structure):
        _fields_ = [("cxLeftWidth", ctypes.c_int),
                    ("cxRightWidth", ctypes.c_int),
                    ("cyTopHeight", ctypes.c_int),
                    ("cyBottomHeight", ctypes.c_int)]

    m = Margins(-1, -1, -1, -1)
    ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(m))


def _set_acrylic(root, tint_bgr: int = 0x292929) -> bool:
    """Acrylic 亚克力材质（配方与开源库 pywinstyles 一致，已实测有效）。

    要点（缺一不可，此前缺 2/3 导致"成功但无效果"）：
      1. 窗口背景涂纯黑且**不能**用 -transparentcolor（颜色键会让材质失效）；
      2. SetWindowCompositionAttribute 用**未公开属性 30** + AccentState=3 + 底色；
      3. DwmExtendFrameIntoClientArea(-1) 把玻璃帧扩到整个客户区。
    tint_bgr 是 0x00BBGGRR 叠加底色（建议取主题 PANEL 色）。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        class AccentPolicy(ctypes.Structure):
            _fields_ = [("AccentState", ctypes.c_uint32),
                        ("AccentFlags", ctypes.c_uint32),
                        ("GradientColor", ctypes.c_uint32),
                        ("AnimationId", ctypes.c_uint32)]

        class WinCompAttrData(ctypes.Structure):
            _fields_ = [("Attribute", ctypes.c_uint32),
                        ("Data", ctypes.c_void_p),
                        ("SizeOfData", ctypes.c_size_t)]

        hwnd = _hwnd_of(root)
        if not hwnd:
            return False
        # 沉浸式深色模式（DWMWA_USE_IMMERSIVE_DARK_MODE）
        dark = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark),
                                                   ctypes.sizeof(dark))
        # 未公开属性 30：Acrylic 表面；AccentState 3；底色 tint_bgr
        policy = AccentPolicy(3, 0, tint_bgr, 0)
        data = WinCompAttrData(30, ctypes.addressof(policy),
                               ctypes.sizeof(policy))
        ok = bool(ctypes.windll.user32.SetWindowCompositionAttribute(
            hwnd, ctypes.byref(data)))
        _extend_frame(hwnd)            # 不扩帧材质无渲染面
        return ok
    except Exception:  # noqa: BLE001
        return False


# 子窗口颜色键：给 Canvas 等子控件单独抠透明，不影响主窗口的 Acrylic
_WS_EX_LAYERED = 0x80000
_LWA_COLORKEY = 1
_GWL_EXSTYLE = -20


def _set_child_colorkey(child_hwnd: int, color_bgr: int = 0x000000) -> bool:
    """给子窗口（如 Tk Canvas）单独加 WS_EX_LAYERED + 颜色键。

    主窗口不能用 -transparentcolor（会破坏 Acrylic），但 Canvas 的不透明
    黑底会挡住 Acrylic。解决：只给 Canvas 子窗口挂颜色键，黑色像素变透明，
    主窗口的 Acrylic 磨砂面就透出来了。color_bgr 是 0x00BBGGRR。
    """
    if os.name != "nt" or not child_hwnd:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        ex = user32.GetWindowLongW(child_hwnd, _GWL_EXSTYLE)
        user32.SetWindowLongW(child_hwnd, _GWL_EXSTYLE, ex | _WS_EX_LAYERED)
        return bool(user32.SetLayeredWindowAttributes(
            child_hwnd, color_bgr, 0, _LWA_COLORKEY))
    except Exception:  # noqa: BLE001
        return False
