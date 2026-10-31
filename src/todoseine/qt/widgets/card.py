"""圆角卡片与勾选点 —— 看板的基础视觉元素（QPainter 手绘 + 插值动画）。

RoundedCard  圆角底卡，hover / pressed 三档底色随动画平滑过渡；
CheckDot     18px 方形勾选点，勾选进度带弹簧式动画。
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from . import theme


class RoundedCard(QWidget):
    """圆角卡片：负责画底，不负责布局，子类往里加控件即可。"""

    def __init__(self, parent: QWidget | None = None, *, radius: int = 12,
                 bg: QColor | None = None, border: QColor | None = None,
                 hover: QColor | None = None, pressed: QColor | None = None,
                 animated: bool = True):
        super().__init__(parent)
        self.radius = radius
        self.bg = QColor(bg if bg is not None else theme.CARD_BG)
        self.border_color = QColor(border if border is not None else theme.CARD_BORDER)
        self.hover_color = QColor(hover if hover is not None else theme.CARD_HOVER)
        self.pressed_color = QColor(pressed if pressed is not None else theme.CARD_PRESSED)
        self._state = 0.0          # 0=常态 1=hover 2=pressed 的插值位置（连续值）
        self._target = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(theme.motion_ms(140 if animated else 0))
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    # ---------- 状态动画 ----------

    def _animate_to(self, target: float) -> None:
        self._target = target
        self._anim.stop()
        self._anim.setStartValue(float(self._state))
        self._anim.setEndValue(float(target))
        self._anim.start()

    def _on_anim(self, value) -> None:
        self._state = float(value)
        self.update()

    def set_state(self, state: int) -> None:
        """外部（如列表选中态）直接指定 0/1/2 三档。"""
        self._animate_to(float(state))

    def enterEvent(self, event) -> None:  # noqa: N802
        self._animate_to(max(1.0, self._target))
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._animate_to(0.0)
        super().leaveEvent(event)

    # ---------- 绘制 ----------

    def card_color(self) -> QColor:
        if self._state <= 1.0:
            return theme.mix(self.bg, self.hover_color, self._state)
        return theme.mix(self.hover_color, self.pressed_color, self._state - 1.0)

    def border_pen_color(self) -> QColor:
        """hover 只把 1px 细线的透明度从 7% 提到 12%，不改色相。

        规范：层级靠微明度，不靠硬边框。掺主色会让 alpha 一下翻三倍，
        视觉上就变成一条"绿描边"，所以这里只动 alpha。
        """
        t = min(1.0, self._state)
        color = QColor(self.border_color)
        color.setAlpha(round(self.border_color.alpha()
                             + (theme.LINE_SOFT.alpha() - self.border_color.alpha()) * t))
        return color

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, self.radius, self.radius)
        p.fillPath(path, self.card_color())
        p.setPen(QPen(self.border_pen_color(), 1))
        p.drawPath(path)
        self.paint_extra(p)
        p.end()

    def paint_extra(self, painter: QPainter) -> None:
        """子类在卡片底之上补充绘制（强调条 / 焦点框等）。"""

    def paint_focus_ring(self, painter: QPainter) -> None:
        """双环聚焦：内环 1px 低饱和主色 + 外环 3px 22% 柔光（Qt 无 box-shadow 的等价画法）。"""
        if not self.hasFocus():
            return
        outer = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        outer_path = QPainterPath()
        r = max(2, self.radius)
        outer_path.addRoundedRect(outer, r, r)
        glow = QColor(theme.ACCENT)
        glow.setAlphaF(0.22)
        painter.setPen(QPen(glow, 3))
        painter.drawPath(outer_path)

        inner = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        inner_path = QPainterPath()
        inner_path.addRoundedRect(inner, max(2, r - 1), max(2, r - 1))
        painter.setPen(QPen(theme.ACCENT, 1))
        painter.drawPath(inner_path)

    def rounded_path(self, inset: float = 0.0) -> QPainterPath:
        rect = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        path = QPainterPath()
        path.addRoundedRect(rect, self.radius, self.radius)
        return path


class CheckDot(QWidget):
    """方形勾选点：未选中是描边空心，选中后填充强调色并画 ✓。"""

    def __init__(self, parent: QWidget | None = None, size: int = 18):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._checked = False
        self._t = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(theme.motion_ms(150))
        self._anim.setEasingCurve(QEasingCurve.Type.OutBack)
        self._anim.valueChanged.connect(self._on_anim)
        # 按压挤压：按下瞬间整体缩小，松开弹回（对应「先收成圆」的手感）
        self._press = 0.0
        self._press_anim = QVariantAnimation(self)
        self._press_anim.setDuration(theme.motion_ms(110))
        self._press_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._press_anim.valueChanged.connect(self._on_press_anim)

    def set_press(self, pressed: bool) -> None:
        """按压反馈：按下=1，松开=0，带动整体缩放。"""
        target = 1.0 if pressed else 0.0
        self._press_anim.stop()
        self._press_anim.setStartValue(self._press)
        self._press_anim.setEndValue(target)
        self._press_anim.start()

    def _on_press_anim(self, value) -> None:
        self._press = float(value)
        self.update()

    def isChecked(self) -> bool:  # noqa: N802
        return self._checked

    def setChecked(self, checked: bool, animated: bool = True) -> None:  # noqa: N802
        self._checked = bool(checked)
        target = 1.0 if checked else 0.0
        self._anim.stop()
        if not animated:
            self._t = target
            self.update()
            return
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(target)
        self._anim.start()

    def _on_anim(self, value) -> None:
        self._t = float(value)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        t = max(0.0, min(1.0, self._t))
        rect = QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75)
        # 挤压：按下时整体向中心缩小到 0.82 倍
        if self._press > 0.001:
            scale = 1.0 - 0.18 * self._press
            center = rect.center()
            rect = QRectF(center.x() - rect.width() / 2 * scale,
                          center.y() - rect.height() / 2 * scale,
                          rect.width() * scale, rect.height() * scale)

        path = QPainterPath()
        path.addRoundedRect(rect, 5, 5)
        if t > 0:
            fill = QColor(theme.ACCENT)
            fill.setAlphaF(t)
            p.fillPath(path, fill)
        p.setPen(QPen(theme.mix(QColor("#6d6c74"), theme.ACCENT, t), 1))
        p.drawPath(path)

        if t > 0.02:
            # ✓ 笔画动画：先画短臂（左下→底点），再画长臂（底点→右上），
            # 进度按比例切开，取消勾选时沿原路收回。
            pen_color = QColor(theme.ACCENT_TEXT)
            pen_color.setAlphaF(min(1.0, t * 3.0))
            p.setPen(QPen(pen_color, 2.0, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            w, h = rect.width(), rect.height()
            tl = rect.topLeft()
            pt0 = tl + QPointF(w * 0.26, h * 0.52)
            pt1 = tl + QPointF(w * 0.44, h * 0.68)
            pt2 = tl + QPointF(w * 0.74, h * 0.30)
            prog = min(1.0, t)
            first = min(1.0, prog / 0.4)
            p.drawLine(pt0, pt0 + (pt1 - pt0) * first)
            if prog > 0.4:
                second = min(1.0, (prog - 0.4) / 0.6)
                p.drawLine(pt1, pt1 + (pt2 - pt1) * second)
        p.end()
