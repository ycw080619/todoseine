"""全局热键（Win32 RegisterHotKey）。

Qt 主线程自带消息循环，所以只挂一个 QAbstractNativeEventFilter 就能收到
WM_HOTKEY，不需要另开线程。注册失败（被别的软件占用）时返回 False，
调用方据此提示用户换一个组合。

键名写法沿用配置里的字符串：`ctrl+alt+d`、`ctrl+shift+s`、
`win+shift+a`，大小写不敏感。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Callable, Dict, Optional, Tuple

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication

_WM_HOTKEY = 0x0312

_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000

_MODS = {
    "ctrl": _MOD_CONTROL,
    "control": _MOD_CONTROL,
    "alt": _MOD_ALT,
    "shift": _MOD_SHIFT,
    "win": _MOD_WIN,
    "meta": _MOD_WIN,
}

# 单键名 → 虚拟键码（字母数字走 VkKeyScanW，这里只补特殊键）
_NAMED_KEYS = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "`": 0xC0,
}


def parse(spec: str) -> Optional[Tuple[int, int]]:
    """`ctrl+alt+d` → (modifiers, vk)；解析不出来返回 None。"""
    if not spec:
        return None
    parts = [p.strip().lower() for p in str(spec).replace(" ", "").split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    key = ""
    for part in parts:
        if part in _MODS:
            mods |= _MODS[part]
        else:
            key = part
    if not key:
        return None
    vk = _NAMED_KEYS.get(key)
    if vk is None:
        if len(key) != 1:
            return None
        # VkKeyScanW 返回低字节 = 虚拟键码，高字节 = shift 状态
        scan = ctypes.windll.user32.VkKeyScanW(ord(key.upper()))
        if scan == -1:
            return None
        vk = scan & 0xFF
    return mods | _MOD_NOREPEAT, vk


class HotkeyManager(QAbstractNativeEventFilter):
    """注册表：id → 回调。同一组合重复注册会先注销再注册。"""

    def __init__(self, parent: Optional[QCoreApplication] = None):
        super().__init__()
        self._callbacks: Dict[int, Callable[[], None]] = {}
        self._specs: Dict[int, str] = {}
        self._next_id = 0xA000
        self._installed = False

    # ---------- 注册 ----------

    def bind(self, spec: str, callback: Callable[[], None]) -> bool:
        """绑定一个热键；重复绑同一组合视为更新回调。"""
        self.unbind(spec)
        parsed = parse(spec)
        if parsed is None:
            return False
        mods, vk = parsed
        hotkey_id = self._next_id
        self._next_id += 1
        if not ctypes.windll.user32.RegisterHotKey(None, hotkey_id, mods, vk):
            return False
        self._callbacks[hotkey_id] = callback
        self._specs[hotkey_id] = spec
        self._ensure_filter()
        return True

    def unbind(self, spec: str) -> None:
        for hotkey_id, bound in list(self._specs.items()):
            if bound == spec:
                ctypes.windll.user32.UnregisterHotKey(None, hotkey_id)
                self._callbacks.pop(hotkey_id, None)
                self._specs.pop(hotkey_id, None)

    def unbind_all(self) -> None:
        for hotkey_id in list(self._callbacks):
            ctypes.windll.user32.UnregisterHotKey(None, hotkey_id)
        self._callbacks.clear()
        self._specs.clear()
        self._next_id = 0xA000      # id 回收，避免反复保存配置把 id 耗尽

    def _ensure_filter(self) -> None:
        if self._installed:
            return
        app = QCoreApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self)
            self._installed = True

    # ---------- 消息分发 ----------

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        if event_type != b"windows_generic_MSG":
            return False, 0
        try:
            msg = wintypes.MSG.from_address(int(message))
        except Exception:  # noqa: BLE001
            return False, 0
        if msg.message != _WM_HOTKEY:
            return False, 0
        callback = self._callbacks.get(int(msg.wParam))
        if callback is None:
            return False, 0
        callback()
        return True, 0
