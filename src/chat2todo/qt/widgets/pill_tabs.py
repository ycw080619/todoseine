"""分段筛选控件 —— PyQt-Fluent-Widgets SegmentedWidget 的等价物。

深色胶囊轨道 + 强调色实心 pill（滑动动画）+ 选中项深色文字，
顺带显示每页的数量，键盘左右键可切换。
"""

from __future__ import annotations

from typing import Iterable, List, Tuple

from PySide6.QtCore import (QEasingCurve, QRectF, Qt, QVariantAnimation, Signal)
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from . import theme


class PillTabs(QWidget):
    """items 形如 [("todo", "全部"), ("star", "重点"), ...]。"""

    changed = Signal(str)

    def __init__(self, items: Iterable[Tuple[str, str]], parent: QWidget | None = None):
        super().__init__(parent)
        self._items: List[Tuple[str, str]] = list(items)
        self._counts: dict = {}
        self._current = self._items[0][0] if self._items else ""
        self._pill_x = 0.0
        self._pill_index = 0
        self._started = False
        self.setFixedHeight(36)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self._hover_index = -1
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    # ---------- 数据接口 ----------

    def current_key(self) -> str:
        return self._current

    def set_counts(self, counts: dict) -> None:
        self._counts = dict(counts)
        self.update()

    def set_current(self, key: str, animate: bool = True) -> None:
        keys = [k for k, _ in self._items]
        if key not in keys:
            return
        index = keys.index(key)
        if index == self._pill_index and self._started:
            self._current = key
            return
        self._current = key
        self._pill_index = index
        target = self._segment_rect(index).x()
        if not animate or not self._started:
            self._pill_x = target
            self._started = True
            self.update()
            return
        self._anim.stop()
        self._anim.setStartValue(float(self._pill_x))
        self._anim.setEndValue(float(target))
        self._anim.start()

    def _on_anim(self, value) -> None:
        self._pill_x = float(value)
        self.update()

    # ---------- 几何 ----------

    def _segment_width(self) -> float:
        if not self._items:
            return 0.0
        return self.width() / len(self._items)

    def _segment_rect(self, index: int) -> QRectF:
        seg = self._segment_width()
        inset = 3.0
        return QRectF(index * seg + inset, 3.0, max(0.0, seg - 2 * inset),
                      self.height() - 6.0)

    # ---------- 交互 ----------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if not self._started:
            self._pill_x = self._segment_rect(self._pill_index).x()
            self._started = True
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self._items:
            return
        index = min(len(self._items) - 1,
                    max(0, int(event.position().x() // max(1.0, self._segment_width()))))
        key = self._items[index][0]
        if key != self._current:
            self.set_current(key)
            self.changed.emit(key)
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._items:
            return
        index = min(len(self._items) - 1,
                    max(0, int(event.position().x() // max(1.0, self._segment_width()))))
        if index != self._hover_index:
            self._hover_index = index
            self.update()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        keys = [k for k, _ in self._items]
        if not keys:
            return
        delta = 0
        if event.key() == Qt.Key.Key_Left:
            delta = -1
        elif event.key() == Qt.Key.Key_Right:
            delta = 1
        if delta:
            index = (keys.index(self._current) + delta) % len(keys)
            self.set_current(keys[index])
            self.changed.emit(keys[index])
            return
        super().keyPressEvent(event)

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._items:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        track = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        track_path = QPainterPath()
        track_path.addRoundedRect(track, 12, 12)
        p.fillPath(track_path, theme.CARD_BG)

        # 强调色 pill（位置由动画驱动）
        seg = self._segment_width()
        pill = QRectF(self._pill_x, 3.0, max(0.0, seg - 6.0), self.height() - 6.0)
        pill_path = QPainterPath()
        pill_path.addRoundedRect(pill, 10, 10)
        p.fillPath(pill_path, theme.ACCENT)

        for index, (key, label) in enumerate(self._items):
            n = self._counts.get(key)
            text = f"{label} {n}" if n else label
            active = key == self._current
            if not active and index == self._hover_index:
                hover_path = QPainterPath()
                hover_path.addRoundedRect(self._segment_rect(index), 10, 10)
                p.fillPath(hover_path, theme.CARD_HOVER)
            f = QFont(theme.FONT_FAMILY)
            f.setPixelSize(12)
            f.setBold(active)
            p.setFont(f)
            p.setPen(theme.ACCENT_TEXT if active else theme.SUB)
            rect = self._segment_rect(index)
            fm = QFontMetrics(f)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                       fm.elidedText(text, Qt.TextElideMode.ElideRight,
                                     int(rect.width()) - 4))
        if self.hasFocus():
            p.setPen(QPen(theme.ACCENT, 1))
            p.drawPath(track_path)
        p.end()
