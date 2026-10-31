"""PySide6 看板统一配色（对齐 docs/ui/设计规范-组件与交互.md 的微明度令牌）。

只放颜色常量与颜色插值工具，不引入任何界面类，方便各处复用。

令牌口径（暗色，本程序只有暗色一套实盘）：
  - 层级只靠微明度差：面板 → 卡片 ΔL 2.4%，卡片 → hover ΔL 3.0%；
  - 分隔线一律 1px、白 7% 透明度，不使用高对比硬边框；
  - 暗底字重比浅色降一档（正文 400、标题 550），规避字形外溢光晕；
  - 数字统一 tabular-nums，防止计时/日期跳动。
"""

from __future__ import annotations

import os

from PySide6.QtGui import QColor, QFont

# —— 表面阶梯（暗色）——
# L 值：panel 10.4% / card 12.7% / hover 15.7%
PANEL = QColor("#171A1E")
CARD_BG = QColor("#1C2025")
CARD_HOVER = QColor("#23272D")
CARD_PRESSED = QColor("#262B31")

# —— 细线：1px、7%~12% 白，只做分隔不承担层级 ——
HAIRLINE = 0.07
LINE_HAIRLINE = QColor(255, 255, 255, int(255 * HAIRLINE))
LINE_SOFT = QColor(255, 255, 255, int(255 * 0.12))
BORDER = LINE_HAIRLINE
CARD_BORDER = LINE_HAIRLINE
CARD_DIVIDER = QColor(255, 255, 255, int(255 * 0.055))

# —— 文字层级（对比度：14.3:1 / 7.9:1 / 4.5:1 on CARD_BG）——
TEXT = QColor("#E7E9EC")
SUB = QColor("#A9AFB8")
CAPTION = QColor("#878E98")

# —— 强调色：低饱和，只做标记与聚焦 ——
ACCENT = QColor("#6FA893")
ACCENT_HINT = QColor("#8FC5B1")          # 主色上的可读文字色（6.4:1）
ACCENT_TEXT = QColor("#0B0F0D")
ACCENT_SOFT = QColor(111, 168, 147, 31)  # 主色 12%：标签底 / 列表选中底
WARN = QColor("#E0C57A")
DANGER = QColor("#E08C8C")

# —— 字重：暗色降一档 ——
WEIGHT_BODY = 400
WEIGHT_CAPTION = 400
WEIGHT_TITLE = 550

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


def css(color: QColor) -> str:
    """QSS 里要的 rgba() 串；不透明色给 #RRGGBB，省字节也更好读。"""
    if color.alpha() >= 255:
        return color.name()
    return f"rgba({color.red()},{color.green()},{color.blue()},{color.alpha()})"


def qt_weight(value: int) -> QFont.Weight:
    """把设计令牌的字重值映射到 Qt 的字重枚举。

    Qt（PySide6 6.12）的 setWeight 只接受 QFont.Weight 枚举，没有 550 这一档，
    所以 550 向上落到 DemiBold(600)——规范允许的上限正好是 600。
    """
    if value < 450:
        return QFont.Weight.Normal
    if value < 550:
        return QFont.Weight.Medium
    return QFont.Weight.DemiBold


def tabular(font: QFont) -> QFont:
    """锁死数字位宽。

    两条路同时上：
      1) 打开 OpenType 的 tnum/lnum 特性（字体支持才生效，Qt 6.7+ 有 API）；
      2) 兜底把 Consolas 排进字体族首位——数字走 Consolas 必然等宽，
         中文仍由雅黑回退渲染，混排文字不会变成豆腐块。
    实测：微软雅黑 UI 的 0-9 步进本身就是 8px，天然等宽，这里只是双保险。
    """
    font.setFamilies(["Consolas", FONT_FAMILY])
    try:
        font.setFeature(QFont.Tag("tnum"), 1)
        font.setFeature(QFont.Tag("lnum"), 1)
    except Exception:  # noqa: BLE001 - Qt < 6.7 没有 Tag，第一条兜底已生效
        pass
    return font


def body_font(point_size: int = 10) -> QFont:
    """正文/说明字体：暗色字重 400。"""
    f = QFont(FONT_FAMILY)
    f.setPointSize(point_size)
    f.setWeight(qt_weight(WEIGHT_BODY))
    return f


def title_font(point_size: int = 10) -> QFont:
    """标题字体：暗色字重 550（Qt 落到 600，仍不超过规范上限）。"""
    f = QFont(FONT_FAMILY)
    f.setPointSize(point_size)
    f.setWeight(qt_weight(WEIGHT_TITLE))
    return f


def number_font(point_size: int = 9) -> QFont:
    """数字字体：等宽数字，用于时间/金额/统计列。"""
    return tabular(body_font(point_size))


# —— 动效：尊重系统「显示动画」开关 ——

#: SPI_GETANIMATION：读「辅助功能 → 视觉效果 → 动画效果」的落地值
_SPI_GETANIMATION = 0x0048


def reduced_motion() -> bool:
    """系统关掉了「在 Windows 中显示动画」时返回 True。

    走 SystemParametersInfo(SPI_GETANIMATION) 读 iMinAnimate：这正是
    「辅助功能 → 视觉效果 → 动画效果」开关的底层值，比读注册表更可靠。
    非 Windows 或调用失败一律按「动画开启」处理，不改变原有观感。
    每次现读（一次调用约 1~2 µs），用户中途改设置也能立刻生效。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class _AnimationInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("iMinAnimate", wintypes.INT)]

        info = _AnimationInfo()
        info.cbSize = ctypes.sizeof(info)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            _SPI_GETANIMATION, ctypes.sizeof(info), ctypes.byref(info), 0)
        if not ok:
            return False
        return info.iMinAnimate == 0
    except Exception:  # noqa: BLE001 - 读系统设置失败不该影响界面
        return False


#: 动画速度倍率：由设置页的「动画速度」写入（off / fast / normal / slow）
_MOTION_SCALE = 1.0


def set_motion_scale(speed: str) -> None:
    """设置动画速度档位（跟着 DeskBox 的 widgetAnimationSpeed 口径）。

    off=彻底不动，fast=0.6 倍时长，normal=1 倍，slow=1.5 倍。
    """
    global _MOTION_SCALE
    _MOTION_SCALE = {
        "off": 0.0,
        "fast": 0.6,
        "normal": 1.0,
        "slow": 1.5,
    }.get(str(speed or "normal").strip().lower(), 1.0)


def motion_ms(ms: int) -> int:
    """动效时长按系统开关 + 速度档位降级。"""
    if reduced_motion() or _MOTION_SCALE <= 0:
        return 0
    return int(ms * _MOTION_SCALE)
