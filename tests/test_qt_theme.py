"""看板设计令牌的回归测试（对齐 docs/ui/设计规范-组件与交互.md）。

只校验可量化项：对比度、层级明度差、字重降档、等宽数字。
交互手感（拖拽咬合、回弹过冲）无法在单测里断言，留给验收清单人工确认。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QFontMetrics  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from todoseine.qt.widgets import theme  # noqa: E402

# QFontMetrics 需要 QGuiApplication，否则在 Windows 上会直接崩进程（不是抛异常）
_APP = QApplication.instance() or QApplication([])


def _luminance(color: QColor) -> float:
    """WCAG 2.1 的相对亮度。"""
    def chan(v: int) -> float:
        c = v / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * chan(color.red()) + 0.7152 * chan(color.green()) + 0.0722 * chan(color.blue())


def contrast(fg: QColor, bg: QColor) -> float:
    hi, lo = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _lightness(color: QColor) -> float:
    """HSL 的 L（与规范文档口径一致）。"""
    cs = [color.red(), color.green(), color.blue()]
    return (max(cs) + min(cs)) / 2 / 255 * 100


class TestTokens(unittest.TestCase):
    def test_text_contrast_meets_wcag_aa(self):
        # 最差底色是卡片底 CARD_BG，所有文字都要在它上面 ≥4.5:1
        for name, color in (("TEXT", theme.TEXT), ("SUB", theme.SUB), ("CAPTION", theme.CAPTION)):
            ratio = contrast(color, theme.CARD_BG)
            self.assertGreaterEqual(ratio, 4.5, f"{name} 对比度 {ratio:.2f} 未达 AA")

    def test_layer_delta_stays_micro(self):
        # 相邻层只允许 1.5%~3% 的明度差，超了就是"硬分层"
        deltas = {
            "panel_to_card": abs(_lightness(theme.CARD_BG) - _lightness(theme.PANEL)),
            "card_to_hover": abs(_lightness(theme.CARD_HOVER) - _lightness(theme.CARD_BG)),
        }
        for where, delta in deltas.items():
            self.assertGreaterEqual(delta, 1.5, f"{where} 明度差 {delta:.1f}% 太小，看不出层级")
            self.assertLessEqual(delta, 3.0, f"{where} 明度差 {delta:.1f}% 超出微明度上限")

    def test_dark_mode_weights_step_down(self):
        # 暗底字重比浅色降一档：正文 400、标题不超过 600
        self.assertEqual(theme.WEIGHT_BODY, 400)
        self.assertEqual(theme.WEIGHT_CAPTION, 400)
        self.assertLessEqual(theme.WEIGHT_TITLE, 600)
        self.assertGreater(theme.WEIGHT_TITLE, theme.WEIGHT_BODY)

    def test_fonts_carry_expected_weight(self):
        # Qt 没有 550 这一档，令牌 550 落到 DemiBold(600)，仍是规范允许的上限
        self.assertEqual(theme.body_font(10).weight(), theme.qt_weight(theme.WEIGHT_BODY))
        self.assertEqual(theme.title_font(10).weight(), theme.qt_weight(theme.WEIGHT_TITLE))
        self.assertLessEqual(theme.title_font(10).weight().value, 600)

    def test_hairline_is_translucent_white(self):
        # 分隔线只允许细、半透明；不透明线就是"硬边框"
        self.assertEqual(theme.LINE_HAIRLINE.alpha(), int(255 * theme.HAIRLINE))
        self.assertLess(theme.LINE_HAIRLINE.alpha(), 40)
        self.assertEqual(theme.CARD_BORDER.alpha(), theme.LINE_HAIRLINE.alpha())

    def test_number_font_is_visually_tabular(self):
        # 真正要保证的是"1 与 8 的步进宽度相同"，而不是有没有设上 tnum 标志
        font = theme.number_font(10)
        metrics = QFontMetrics(font)
        widths = {metrics.horizontalAdvance(ch) for ch in "1118888"}
        self.assertEqual(len(widths), 1, f"数字宽度不等：{sorted(widths)}")

    def test_css_helper_keeps_alpha(self):
        self.assertEqual(theme.css(theme.TEXT), theme.TEXT.name())
        self.assertTrue(theme.css(theme.LINE_HAIRLINE).startswith("rgba("))


class TestMotion(unittest.TestCase):
    """系统「显示动画」关闭时，动效时长必须归零（直接落位 / 直接淡出）。"""

    def setUp(self):
        # 速度档位是模块级状态，先归位避免用例间互相污染
        theme.set_motion_scale("normal")

    def test_reduced_motion_returns_bool(self):
        self.assertIsInstance(theme.reduced_motion(), bool)

    def test_motion_ms_zeroes_when_reduced(self):
        with mock.patch.object(theme, "reduced_motion", return_value=True):
            self.assertEqual(theme.motion_ms(180), 0)
            self.assertEqual(theme.motion_ms(0), 0)

    def test_motion_ms_keeps_token_when_allowed(self):
        with mock.patch.object(theme, "reduced_motion", return_value=False):
            self.assertEqual(theme.motion_ms(180), 180)
            self.assertEqual(theme.motion_ms(160), 160)

    def test_motion_scale_off_zeroes_duration(self):
        theme.set_motion_scale("off")
        with mock.patch.object(theme, "reduced_motion", return_value=False):
            self.assertEqual(theme.motion_ms(180), 0)

    def test_motion_scale_fast_and_slow(self):
        with mock.patch.object(theme, "reduced_motion", return_value=False):
            theme.set_motion_scale("fast")
            self.assertEqual(theme.motion_ms(200), 120)
            theme.set_motion_scale("slow")
            self.assertEqual(theme.motion_ms(200), 300)

    def test_motion_scale_unknown_falls_back_to_normal(self):
        theme.set_motion_scale("bogus")
        with mock.patch.object(theme, "reduced_motion", return_value=False):
            self.assertEqual(theme.motion_ms(200), 200)


class TestControlStyles(unittest.TestCase):
    """下拉框 / 勾选框的 QSS 口径：细线、无硬边框、展开不改变宽度。"""

    def setUp(self):
        from todoseine.qt import settings_panel

        self.combo = settings_panel._COMBO_STYLE  # noqa: SLF001 - 直接校验样式串
        self.check = settings_panel._CHECK_STYLE  # noqa: SLF001

    def test_combo_border_is_hairline_only(self):
        # 只允许 1px 细线；出现 2px 以上就是"硬边框"
        self.assertIn("border: 1px solid", self.combo)
        for bad in ("border: 2px", "border: 3px", "border: 4px"):
            self.assertNotIn(bad, self.combo)

    def test_combo_popup_uses_token_colors(self):
        self.assertIn(theme.CARD_BG.name(), self.combo)
        self.assertIn(theme.css(theme.ACCENT), self.combo)
        self.assertIn(theme.css(theme.ACCENT_SOFT), self.combo)

    def test_checkbox_indicator_matches_tokens(self):
        self.assertIn("width: 14px", self.check)
        self.assertIn(theme.ACCENT.name(), self.check)
        self.assertIn(theme.css(theme.LINE_SOFT), self.check)
