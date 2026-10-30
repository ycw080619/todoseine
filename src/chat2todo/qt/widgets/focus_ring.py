"""输入框双环聚焦（Qt 等价实现）。

规范要求：内环 1px 低饱和主色、外环 2~4px 柔光扩散、过渡 120~180ms，
且聚焦不改变布局尺寸、不出现生硬描边。

Qt 没有 CSS 的 box-shadow，这里拆成两件事：
  - 内环：QSS 里把边框宽度恒定为 1px，聚焦只换颜色（不撑大控件）；
  - 外环：QGraphicsDropShadowEffect（offset=0、blur=10、主色 30% 透明度），
    只在聚焦时启用，失焦立刻关掉，避免整窗持续走合成层。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QWidget

from . import theme


class FocusGlow(QObject):
    """给任意输入类控件挂上"外环柔光"，聚焦进出自动开关。"""

    def __init__(self, widget: QWidget, color: QColor | None = None,
                 blur: int = 10, alpha: float = 0.30):
        super().__init__(widget)
        self._widget = widget
        self._effect = QGraphicsDropShadowEffect(widget)
        self._effect.setBlurRadius(blur)
        self._effect.setOffset(0, 0)
        glow = QColor(color or theme.ACCENT)
        glow.setAlphaF(alpha)
        self._effect.setColor(glow)
        self._effect.setEnabled(False)
        widget.setGraphicsEffect(self._effect)
        widget.installEventFilter(self)

    @property
    def effect(self) -> QGraphicsDropShadowEffect:
        return self._effect

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 接口
        if obj is self._widget:
            if event.type() == QEvent.Type.FocusIn:
                self._effect.setEnabled(True)
            elif event.type() == QEvent.Type.FocusOut:
                self._effect.setEnabled(False)
        return super().eventFilter(obj, event)


def attach(widget: QWidget, color: QColor | None = None) -> FocusGlow:
    """挂载外环柔光并返回句柄（持有引用，防止被回收）。

    控件自身要带透明的圆角背景，阴影才会贴着轮廓散开；
    给 QLineEdit 用 QSS `background: transparent` + 父容器负责底色即可。
    """
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    return FocusGlow(widget, color=color)
