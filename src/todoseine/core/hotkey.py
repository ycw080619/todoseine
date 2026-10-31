"""全局热键的字符串解析（零依赖，供看板与截图插件共用）。

支持写法：`ctrl+alt+d`、`Ctrl+Shift+F5`、`win+space`。
必须至少含一个修饰键（ctrl/alt/shift/win），否则返回 None。
"""

from __future__ import annotations

from typing import Optional, Tuple

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_LETTERS = {chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)}
_DIGITS = {str(d): 0x30 + d for d in range(10)}
_FUNC = {f"f{i}": 0x6F + i for i in range(1, 13)}


def parse_hotkey(spec: str) -> Optional[Tuple[int, int]]:
    """`"ctrl+alt+d"` → `(mods|MOD_NOREPEAT, vk)`；无法识别返回 None。"""
    if not spec:
        return None
    mods = 0
    vk = None
    for part in str(spec).lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "meta", "super"):
            mods |= MOD_WIN
        elif part in _LETTERS:
            vk = _LETTERS[part]
        elif part in _DIGITS:
            vk = _DIGITS[part]
        elif part in _FUNC:
            vk = _FUNC[part]
        elif part == "space":
            vk = 0x20
        else:
            return None
    if not vk or not mods:
        return None
    return mods | MOD_NOREPEAT, vk
