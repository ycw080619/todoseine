"""PySide6 看板统一配色（对齐 dashboard/app.py 的 WinUI 3 深色色板）。

只放颜色常量与颜色插值工具，不引入任何界面类，方便各处复用。
"""

from __future__ import annotations

from PySide6.QtGui import QColor

# —— 主面板 / 卡片层级 ——
PANEL = QColor("#212024")
BORDER = QColor("#3a3940")
CARD_BG = QColor("#2c2b31")
CARD_HOVER = QColor("#33323a")
CARD_PRESSED = QColor("#3a3942")
CARD_BORDER = QColor("#3a3940")
CARD_DIVIDER = QColor("#3d3a44")

# —— 文字层级 ——
TEXT = QColor("#f2f2f4")
SUB = QColor("#9c9ca6")
CAPTION = QColor("#6b6873")

# —— 强调色 ——
ACCENT = QColor("#4cc2ff")
ACCENT_TEXT = QColor("#0b1a22")
WARN = QColor("#ffd93d")
DANGER = QColor("#ff8fab")

#: 每条待办左侧竖色条轮换用的颜色
ACCENTS = [QColor(c) for c in
           ("#ff6b6b", "#ff9f43", "#ffd93d", "#6ee7a8",
            "#4cc9f0", "#8aa4ff", "#ff8fab")]

FONT_FAMILY = "Microsoft YaHei UI"


def mix(c1: QColor, c2: QColor, t: float) -> QColor:
    """在两个颜色之间线性插值，t∈[0,1]。"""
    t = max(0.0, min(1.0, float(t)))
    return QColor(
        round(c1.red() + (c2.red() - c1.red()) * t),
        round(c1.green() + (c2.green() - c1.green()) * t),
        round(c1.blue() + (c2.blue() - c1.blue()) * t),
        round(c1.alpha() + (c2.alpha() - c1.alpha()) * t),
    )
