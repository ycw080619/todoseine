"""看板窗口的几何计算：多显示器工作区、钳位、边缘吸附、四角锚点、滑入偏移。

全是纯函数，不碰 tkinter / Win32 句柄，无头环境可直接单测。
"""

from __future__ import annotations

import os
from typing import List, Tuple

SNAP_PX = 8            # 吸附阈值：距边缘多少像素内吸上去（DeskBox 默认 5，范围 0-32）


def _ease_out_cubic(t: float) -> float:
    """缓动曲线：起步快、收尾慢（与 DeskBox 的 ease-out power 3 一致）。"""
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _monitor_work_areas() -> List[Tuple[int, int, int, int]]:
    """所有显示器的工作区矩形 [(left, top, right, bottom)]。

    副屏拔掉/换位置后，保存的坐标可能落在虚空里；非 Windows 或枚举
    失败时返回空列表（调用方退回不钳位）。
    """
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]

        user32 = ctypes.windll.user32
        rects: List[Tuple[int, int, int, int]] = []
        callback_t = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(wintypes.RECT), ctypes.c_void_p)

        def _cb(hmon, _hdc, _rect, _lp) -> int:
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                rc = info.rcWork
                rects.append((rc.left, rc.top, rc.right, rc.bottom))
            return 1

        user32.EnumDisplayMonitors(None, None, callback_t(_cb), 0)
        return rects
    except Exception:  # noqa: BLE001
        return []


def _clamp_to_rects(x: int, y: int, w: int, h: int, rects,
                    margin: int = 48) -> Tuple[int, int]:
    """把窗口左上角钳位到工作区并集内，保证至少 margin 像素可见。

    当前位置已经够可见就原样返回；否则贴到最近工作区的边缘收回来。
    """
    if not rects:
        return x, y

    def _visible(nx: int, ny: int) -> bool:
        for left, top, right, bottom in rects:
            if (min(nx + w, right) - max(nx, left) >= margin
                    and min(ny + h, bottom) - max(ny, top) >= margin):
                return True
        return False

    if _visible(x, y):
        return x, y
    cx, cy = x + w // 2, y + h // 2

    def _dist2(rc) -> int:
        left, top, right, bottom = rc
        dx = max(left - cx, 0, cx - right)
        dy = max(top - cy, 0, cy - bottom)
        return dx * dx + dy * dy

    left, top, right, bottom = min(rects, key=_dist2)
    nx = min(max(x, left - w + margin), right - margin)
    ny = min(max(y, top - h + margin), bottom - margin)
    return nx, ny


def _snap_to_edges(x: int, y: int, w: int, h: int, rects,
                   spacing: int = SNAP_PX) -> Tuple[int, int]:
    """拖动松手时，把贴近工作区边缘的窗口吸上去（对齐 DeskBox 的边缘吸附）。

    只吸窗口中心点所在的显示器；中心不在任何工作区时取最近的那个。
    spacing <= 0 或没有工作区信息时原样返回。
    """
    if spacing <= 0 or not rects:
        return x, y
    cx, cy = x + w // 2, y + h // 2
    target = None
    for rc in rects:
        left, top, right, bottom = rc
        if left <= cx < right and top <= cy < bottom:
            target = rc
            break
    if target is None:
        def _dist2(rc) -> int:
            left, top, right, bottom = rc
            dx = max(left - cx, 0, cx - right)
            dy = max(top - cy, 0, cy - bottom)
            return dx * dx + dy * dy

        target = min(rects, key=_dist2)
    left, top, right, bottom = target
    nx, ny = x, y
    if abs(x - left) <= spacing:
        nx = left
    elif abs((x + w) - right) <= spacing:
        nx = right - w
    if abs(y - top) <= spacing:
        ny = top
    elif abs((y + h) - bottom) <= spacing:
        ny = bottom - h
    return nx, ny


def _anchor_of(x: int, y: int, w: int, h: int, rect) -> Tuple[str, int, int]:
    """窗口在 rect 里的四角锚点 + 距锚点的边距（DeskBox 的 PositionAnchor 思路）。

    返回 ("lt"/"rt"/"lb"/"rb", mx, my)：中心偏哪半就锚哪条边，
    边距是窗口到锚边的距离。分辨率不变时 _apply_anchor 能原样还原。
    """
    left, top, right, bottom = rect
    cx, cy = x + w // 2, y + h // 2
    corner = ("r" if cx >= (left + right) / 2 else "l") + \
             ("b" if cy >= (top + bottom) / 2 else "t")
    mx = x - left if corner[0] == "l" else right - (x + w)
    my = y - top if corner[1] == "t" else bottom - (y + h)
    return corner, mx, my


def _apply_anchor(anchor, w: int, h: int, rect) -> Tuple[int, int]:
    """按锚点 + 边距在当前工作区重算位置；换分辨率/换屏后仍贴住原来的角落。"""
    corner, mx, my = anchor
    left, top, right, bottom = rect
    x = left + mx if corner[0] == "l" else right - mx - w
    y = top + my if corner[1] == "t" else bottom - my - h
    return x, y


def _slide_offset(x: int, y: int, w: int, h: int, rects,
                  dist: int = 24) -> Tuple[int, int]:
    """唤出滑入动画的起点：朝中心点所在工作区最近的边缘方向偏移 dist 像素。"""
    if not rects:
        return x, y
    cx, cy = x + w // 2, y + h // 2
    for left, top, right, bottom in rects:
        if left <= cx < right and top <= cy < bottom:
            d = {"l": cx - left, "r": right - cx, "t": cy - top, "b": bottom - cy}
            side = min(d, key=d.get)
            if side == "l":
                return x - dist, y
            if side == "r":
                return x + dist, y
            if side == "t":
                return x, y - dist
            return x, y + dist
    return x, y
