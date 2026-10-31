"""双环聚焦与等宽数字的控件级回归（对齐验收清单 1 / 2 / 8）。

用 offscreen 平台跑：只验证几何与效果开关，不依赖真实字体渲染。
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from todoseine.qt.widgets import focus_ring, theme  # noqa: E402

_APP = QApplication.instance() or QApplication([])


class TestFocusRing(unittest.TestCase):
    def setUp(self):
        self.host = QWidget()
        self.host.resize(300, 80)
        self.host.show()

    def tearDown(self):
        self.host.deleteLater()
        _APP.processEvents()

    def test_glow_toggles_with_focus(self):
        glow = focus_ring.attach(self.host)
        self.assertFalse(glow.effect.isEnabled())
        self.host.setFocus()
        _APP.processEvents()
        self.assertTrue(glow.effect.isEnabled())
        # 失焦必须立刻关掉：否则整窗持续走合成层，拖拽会掉帧
        self.host.clearFocus()
        _APP.processEvents()
        self.assertFalse(glow.effect.isEnabled())

    def test_glow_is_soft_and_centered(self):
        # 外环是柔光扩散：偏移必须为 0，模糊半径落在 2~4px 当量
        glow = focus_ring.attach(self.host)
        self.assertEqual(glow.effect.offset().x(), 0)
        self.assertEqual(glow.effect.offset().y(), 0)
        self.assertGreaterEqual(glow.effect.blurRadius(), 6)
        color = glow.effect.color()
        self.assertEqual((color.red(), color.green(), color.blue()),
                         (theme.ACCENT.red(), theme.ACCENT.green(), theme.ACCENT.blue()))
        self.assertLess(color.alpha(), 255)        # 必须半透明，否则是描边不是柔光

    def test_focus_does_not_change_geometry(self):
        # 验收清单第 1 条：聚焦前后控件宽高差值必须为 0
        glow = focus_ring.attach(self.host)
        before = self.host.size()
        self.host.setFocus()
        _APP.processEvents()
        self.assertEqual(self.host.size(), before)
        self.host.clearFocus()
        _APP.processEvents()
        self.assertEqual(self.host.size(), before)
        self.assertIsNotNone(glow)                 # 句柄要保住，防止效果被回收


class TestThemeHelpers(unittest.TestCase):
    def test_mix_hits_both_ends(self):
        self.assertEqual(theme.mix(theme.PANEL, theme.CARD_BG, 0.0).name(), theme.PANEL.name())
        self.assertEqual(theme.mix(theme.PANEL, theme.CARD_BG, 1.0).name(), theme.CARD_BG.name())

    def test_mix_clamps_out_of_range(self):
        self.assertEqual(theme.mix(theme.PANEL, theme.CARD_BG, -1).name(), theme.PANEL.name())
        self.assertEqual(theme.mix(theme.PANEL, theme.CARD_BG, 2).name(), theme.CARD_BG.name())

    def test_qt_weight_mapping(self):
        from PySide6.QtGui import QFont

        self.assertEqual(theme.qt_weight(400), QFont.Weight.Normal)
        self.assertEqual(theme.qt_weight(500), QFont.Weight.Medium)
        self.assertEqual(theme.qt_weight(550), QFont.Weight.DemiBold)
        self.assertEqual(theme.qt_weight(600).value, 600)


class TestCardHover(unittest.TestCase):
    """hover 只允许提透明度，不许把细线变成彩色描边。"""

    def test_border_hover_only_raises_alpha(self):
        from todoseine.qt.widgets.card import RoundedCard

        card = RoundedCard()
        try:
            rest = card.border_pen_color()
            card._state = 1.0                       # noqa: SLF001 - 直接摆到 hover 终态
            hover = card.border_pen_color()
            # 色相不许变：RGB 必须与静止态完全一致
            self.assertEqual((hover.red(), hover.green(), hover.blue()),
                             (rest.red(), rest.green(), rest.blue()))
            # 透明度只能微调：7% → 12%
            self.assertGreater(hover.alpha(), rest.alpha())
            self.assertLessEqual(hover.alpha(), theme.LINE_SOFT.alpha())
            self.assertLessEqual(hover.alpha(), 40)
        finally:
            card.deleteLater()
            _APP.processEvents()


if __name__ == "__main__":
    unittest.main()
