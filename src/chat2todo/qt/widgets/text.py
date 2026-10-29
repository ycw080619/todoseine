"""单行省略文字标签。

QLabel 在窄容器里会把同排按钮挤出可视区，这里改成按可用宽度补省略号，
并让水平尺寸策略为 Ignored，从而不参与最小宽度协商。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget


class ElidedLabel(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight):
        super().__init__(text, parent)
        self._mode = mode
        self._color: QColor | None = None
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setTextColor(self, color: QColor) -> None:  # noqa: N802
        """自绘不走样式表解析，颜色必须显式传入。"""
        self._color = QColor(color)
        self.update()

    def minimumSizeHint(self):  # noqa: N802
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        metrics = QFontMetrics(self.font())
        text = metrics.elidedText(self.text(), self._mode, self.width())
        color = self._color or self.palette().color(QPalette.ColorRole.WindowText)
        painter.setPen(color)
        painter.drawText(self.rect(), int(self.alignment()), text)
        painter.end()
