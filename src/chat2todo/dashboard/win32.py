"""看板的 Win32 交互：单实例互斥、全局热键、窗口层级、毛玻璃。

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
