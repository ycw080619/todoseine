"""看板内轻提示条（DeskBox 风格 toast）。

贴在父控件底部居中浮出，圆角深色卡 + 可选右侧强调色操作按钮，
淡入 → 停留 → 淡出后自动销毁。
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import (QEasingCurve, QPoint, QPropertyAnimation, Qt, QTimer)
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QPushButton, QWidget)

from . import theme


class Toast(QWidget):
    """不要直接 new，统一走 Toast.show_toast(...)。"""

    def __init__(self, parent: QWidget, text: str, action_text: str = "",
                 on_action: Optional[Callable[[], None]] = None,
                 duration: int = 2600):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._fade_out_started = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 10, 8)
        layout.setSpacing(10)

        label = QLabel(text)
        label.setStyleSheet(
            f"color: {theme.TEXT.name()}; font-size: 12px; background: transparent;"
        )
        layout.addWidget(label, 1)

        if action_text:
            btn = QPushButton(action_text)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none;"
                f" color: {theme.ACCENT.name()}; font-size: 12px; font-weight: 600;"
                " padding: 0 4px; }"
                f"QPushButton:hover {{ color: {theme.TEXT.name()}; }}"
            )
            btn.clicked.connect(self._do_action)
            layout.addWidget(btn)

        # 按文字宽度定尺寸（留出边距，最长 320px）
        fm = QFontMetrics(QFont(theme.FONT_FAMILY, 9))
        extra = 78 if action_text else 40
        width = min(340, max(160, fm.horizontalAdvance(text) + extra))
        self.setFixedSize(width, 36)

        self._on_action = on_action
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(theme.motion_ms(160))
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)
        self._hide_timer.start(max(600, int(duration)))

    # ---------- 生命周期 ----------

    def _present(self) -> None:
        self._reposition()
        self.show()
        self.raise_()
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def _fade_out(self) -> None:
        if self._fade_out_started:
            return
        self._fade_out_started = True
        self._fade.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self.deleteLater)
        self._fade.start()

    def _do_action(self) -> None:
        cb, self._on_action = self._on_action, None
        self._fade_out()
        if cb:
            cb()

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if not parent:
            return
        x = max(12, (parent.width() - self.width()) // 2)
        y = max(12, parent.height() - self.height() - 56)
        self.move(QPoint(x, y))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._reposition()

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        p.fillPath(path, theme.CARD_PRESSED)
        p.setPen(QPen(theme.BORDER, 1))
        p.drawPath(path)
        p.end()

    # ---------- 便捷入口 ----------

    @classmethod
    def show_toast(cls, parent: QWidget, text: str, action_text: str = "",
                   on_action: Optional[Callable[[], None]] = None,
                   duration: int = 2600) -> "Toast":
        toast = cls(parent, text, action_text, on_action, duration)
        toast._present()
        return toast
