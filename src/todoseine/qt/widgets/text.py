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

    def sizeHint(self):  # noqa: N802
        """高度固定为单行。

        QLabel 遇到含换行的文本会把 sizeHint 撑成多行，行高就跟着变，
        列表里高矮不一。这里强制单行高度，宽度交给 Ignored 策略协商。
        """
        hint = super().sizeHint()
        hint.setHeight(QFontMetrics(self.font()).height())
        hint.setWidth(0)
        return hint

    def minimumSizeHint(self):  # noqa: N802
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        hint.setHeight(QFontMetrics(self.font()).height())
        return hint

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        metrics = QFontMetrics(self.font())
        # 原始消息里的换行会让 Qt 按多行绘制，把卡片撑高、文字位置错乱。
        # 这里统一压成空格，并强制单行绘制，只用省略号截断。
        raw = " ".join(str(self.text()).split())
        text = metrics.elidedText(raw, self._mode, self.width())
        color = self._color or self.palette().color(QPalette.ColorRole.WindowText)
        painter.setPen(color)
        painter.drawText(
            self.rect(),
            int(self.alignment()) | int(Qt.TextFlag.TextSingleLine),
            text,
        )
        painter.end()
