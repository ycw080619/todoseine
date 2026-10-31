"""半自动转发（高风险可选功能，默认关闭）。

文档口径：
  * 默认完全关闭，开启需二次确认；
  * 只支持用户手动单次触发，不提供批量 / 定时 / 自动重试；
  * 发送前必须弹预览窗（目标 + 最终内容）；
  * 3 秒倒计时，期间「确认发送」置灰，倒计时结束仍需手动点击；
  * 倒计时期间切换窗口自动取消；
  * 模拟键鼠由后台执行，不抢焦点；
  * 每次尝试都写审计日志（目标、内容哈希、时间、结果），不落原文。

本模块只做与 UI 无关的纯逻辑，Qt 弹窗在 ``qt/forward_dialog.py``。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("todoseine.forward")

CONFIRM_SECONDS = 3          # 文档要求：3 秒倒计时
_MAX_CHARS = 2000            # 单次转发内容上限，防止误发长文本


def enabled(cfg: Dict[str, Any]) -> bool:
    """转发总开关，默认关闭。"""
    return bool(dict(cfg.get("forward") or {}).get("enabled", False))


def audit_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".chat2todo", "forward_audit.jsonl")


def content_hash(text: str) -> str:
    """内容哈希（SHA-256 前 16 位）：审计只记哈希，不记原文。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def prepare(text: str) -> str:
    """规整待发内容：去掉首尾空白并截断到安全长度。"""
    return (text or "").strip()[:_MAX_CHARS]


def rewrite(text: str, cfg: Dict[str, Any]) -> Optional[str]:
    """AI 口吻改写；失败或未启用时返回 None，由调用方回退原文。"""
    text = prepare(text)
    if not text:
        return None
    try:
        from ..ai import AI

        out = AI(cfg).rewrite(text)
    except Exception:  # noqa: BLE001 - 改写是增强项，失败必须回退
        log.warning("AI 改写失败，回退原文", exc_info=True)
        return None
    out = (out or "").strip()
    return out[:_MAX_CHARS] if out else None


def record(target: str, text: str, result: str, error: str = "",
           action: str = "send") -> Dict[str, Any]:
    """写一条审计日志。只记目标、哈希、长度与结果，绝不写原文。

    ``result`` 取值：sent / failed / cancelled / previewed。
    """
    entry = {
        "ts": int(time.time()),
        "action": action,
        "target": (target or "")[:120],
        "hash": content_hash(text),
        "chars": len(prepare(text)),
        "result": result,
        "error": (error or "")[:200],
    }
    path = audit_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("转发审计写入失败：%s", exc)
    return entry


def read_audit(limit: int = 50) -> List[Dict[str, Any]]:
    """读最近的审计记录（调试与测试用）。"""
    path = audit_path()
    if not os.path.isfile(path):
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows[-limit:]


def can_send() -> bool:
    """当前平台是否支持模拟键鼠（本项目只承诺 Windows 11）。"""
    return os.name == "nt"


def simulate_send(text: str) -> bool:
    """把文本用模拟按键打进当前焦点窗口（仅 Windows）。

    刻意不带重试：文档要求「无自动重试」，失败就把控制权交回用户。
    不抢焦点：调用方必须保证目标窗口已在前台，本函数只负责打字。
    """
    text = prepare(text)
    if not text or not can_send():
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001 - 极端环境缺 ctypes，直接判失败
        return False

    ulong_ptr = ctypes.POINTER(ctypes.c_ulong)
    key_event_unicode = 0x0004
    key_event_keyup = 0x0002

    class KeybdInput(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ulong_ptr)]

    class MouseInput(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

    class HardwareInput(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class InputUnion(ctypes.Union):
        _fields_ = [("ki", KeybdInput), ("mi", MouseInput), ("hi", HardwareInput)]

    class Input(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", InputUnion)]

    # Windows 的 Unicode 注入按 UTF-16 码元计；emoji 等增补字符会占两个码元
    data = text.encode("utf-16-le")
    units = [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]

    events = []
    for code in units:
        for flags in (key_event_unicode, key_event_unicode | key_event_keyup):
            ev = Input()
            ev.type = 1  # INPUT_KEYBOARD
            ev.ki = KeybdInput(wVk=0, wScan=code, dwFlags=flags, time=0,
                               dwExtraInfo=None)
            events.append(ev)

    if not events:
        return False
    try:
        array = (Input * len(events))(*events)
        sent = ctypes.windll.user32.SendInput(len(events), array,
                                              ctypes.sizeof(Input))
    except Exception:  # noqa: BLE001 - 系统拒绝注入（UAC / 驱动层拦截）
        log.warning("模拟按键注入失败", exc_info=True)
        return False
    return int(sent) == len(events)
