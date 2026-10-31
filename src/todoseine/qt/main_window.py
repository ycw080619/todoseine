"""PySide6 无边框 QMainWindow —— DeskBox 风格主窗口壳。

职责：
  - 无边框 + DWM 圆角 + Mica/Acrylic 材质
  - PerMonitorV2 高 DPI
  - nativeEvent 处理 WM_NCCALCSIZE（无边框但保留阴影）
  - 标题栏拖拽移动 / 四边四角拖拽改尺寸 / 屏幕边缘吸附 / 锁定位置
  - 位置尺寸记忆（写回配置）
  - 迷你胶囊模式（只留一条状态条）
  - 最小化到托盘

内容区只有「待办 / 回收站」两页；设置、统计、关于都在独立窗口（普通 QMainWindow，
不走半透明分层合成，渲染稳定）。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Optional

from PySide6.QtCore import (QEasingCurve, QEvent, QPoint, QPointF,
                            QPropertyAnimation, QRect, QRectF, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QMouseEvent, QPainter,
                           QPen, QPixmap)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config as c2t_config
from . import dwm
from .widgets import theme

# ---------- Win32 常量 ----------

_WM_NCCALCSIZE = 0x0083
_WM_NCHITTEST = 0x0084
_HTMAXBUTTON = 9
_HTCLIENT = 1
_HTNOWHERE = 0

# ---------- 尺寸阈值 ----------

_EDGE_MARGIN = 7       # 四边 / 四角拖拽改尺寸的像素阈值
_MIN_WIDTH = 460
_MIN_HEIGHT = 420
_MAX_WIDTH = 980
_TITLE_BAR_H = 36

# 拖拽区域 → 鼠标光标：和普通窗口一致
_CURSOR_FOR_EDGE = {
    "left": Qt.CursorShape.SizeHorCursor,
    "right": Qt.CursorShape.SizeHorCursor,
    "top": Qt.CursorShape.SizeVerCursor,
    "bottom": Qt.CursorShape.SizeVerCursor,
    "top-left": Qt.CursorShape.SizeFDiagCursor,
    "bottom-right": Qt.CursorShape.SizeFDiagCursor,
    "top-right": Qt.CursorShape.SizeBDiagCursor,
    "bottom-left": Qt.CursorShape.SizeBDiagCursor,
}
_SNAP_PX = 16          # 距屏幕边缘多少像素内自动吸附
_COMPACT_W = 560       # 窄于这个宽度就藏掉标题文字，把空间让给导航

_MINI_W = 300
_MINI_H = 96

# 贴边自动收起后仍留在屏幕内的像素宽度（鼠标移上去再滑出）
_AUTO_HIDE_PX = 6
_AUTO_HIDE_DELAY = 450

# (页签文字, 页索引) —— 悬浮窗口只承载看板和回收站，
# 设置/统计/关于拆到独立窗口（避开亚克力+半透明渲染路径）
_PAGES = (("待办", 0), ("回收站", 1))


class _CircleRevealOverlay(QWidget):
    """圆形扩散转场覆盖层：盖住旧页截图，从点击点挖出一个不断扩大的圆，露出新页。

    对应视频「圆形扩散转场」：OPEN FROM WHERE YOU TAPPED。
    画在 QStackedWidget 之上（子控件，不透明），避开半透明分层窗口的合成问题。
    """

    def __init__(self, parent: QWidget, old_pixmap: QPixmap, center: QPointF):
        super().__init__(parent)
        self._pix = old_pixmap
        self._center = center
        self._r = 0.0
        # 最远角到中心的距离：圆必须扩到完全覆盖
        w, h = float(old_pixmap.width()), float(old_pixmap.height())
        corners = (QPointF(0, 0), QPointF(w, 0), QPointF(0, h), QPointF(w, h))
        self._max_r = max((c - center).manhattanLength() for c in corners)
        self.setGeometry(old_pixmap.rect())
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # 背景透明：DestinationOut 擦出的圆洞才能透出底下的新页，否则是黑底
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self._anim.finished.connect(self.deleteLater)

    def start(self, ms: int) -> None:
        self.show()
        self.raise_()
        self._anim.setDuration(ms)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(self._max_r)
        self._anim.start()

    def _on_anim(self, value) -> None:
        self._r = float(value)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 旧页垫底
        p.drawPixmap(0, 0, self._pix)
        # 从点击点挖出一个圆：DestinationOut 把圆内擦透明，露出底下的新页
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
        p.setBrush(Qt.BrushStyle.SolidPattern)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(self._center, self._r, self._r)
        p.end()


class FramelessMainWindow(QMainWindow):
    """无边框主窗口壳 —— 只负责悬浮看板 + 回收站。

    设置、统计、关于通过代理信号交给外部打开独立窗口；
    独立窗口都是普通 QMainWindow，不走 WA_TranslucentBackground，
    渲染完全稳定。
    """

    minimized_to_tray = Signal()
    closing = Signal()
    page_changed = Signal(int)
    # 代理信号：悬浮窗口内的齿轮请求打开设置独立窗口
    settings_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        cfg = c2t_config.load()
        self._cfg = cfg
        self._dash_cfg = dict(cfg.get("dash") or {})

        self.setWindowTitle("TodoSeine")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Window
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setMinimumSize(_MIN_WIDTH, _MIN_HEIGHT)

        # ---------- 状态 ----------
        self._drag_pos: Optional[QPoint] = None
        self._resize_edge = ""            # "" / "left" / "right"
        self._resize_start: Optional[tuple] = None
        self._material_applied = False
        self._material_tried = False
        self._mini = False
        self._mini_saved: Optional[QRect] = None
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.setInterval(600)
        self._geo_timer.timeout.connect(self._save_geometry)

        # ---------- 贴边自动收起 / 不抢焦点 ----------
        self._snap_side = ""              # "left" / "right" / "top" / "bottom" / ""
        self._hidden_edge = ""            # 已收起时停在哪个边
        self._visible_pos: Optional[QPoint] = None
        self._noactivate_off = False      # 用户点输入框时临时让出焦点
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(_AUTO_HIDE_DELAY)
        self._hide_timer.timeout.connect(self._auto_hide)
        self._slide_anim = QPropertyAnimation(self, b"pos", self)
        # 时长每次现读系统「动画效果」开关，关掉时直接落位
        self._slide_anim.setDuration(theme.motion_ms(180))
        self._slide_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        # 迷你 ↔ 展开时尺寸同步过渡（和位置动画并行）
        self._size_anim = QPropertyAnimation(self, b"size", self)
        self._size_anim.setDuration(theme.motion_ms(220))
        self._size_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._size_anim.finished.connect(self._on_size_anim_finished)
        # 淡入淡出动效：只动窗口整体不透明度，不碰子控件
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # ---------- 中心 Widget ----------
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._title_bar = self._build_title_bar()
        root.addWidget(self._title_bar)

        from .dashboard import Dashboard
        from .pages import TrashPage

        self._stack = QStackedWidget(central)
        self._dashboard = Dashboard(parent=central)
        self._trash = TrashPage(str(cfg.get("db_path") or ""), central)
        self._mini_panel = self._build_mini_panel()

        for widget in (self._dashboard, self._trash, self._mini_panel):
            self._stack.addWidget(widget)
        root.addWidget(self._stack, 1)

        self._dashboard.settings_requested.connect(self.settings_requested.emit)
        self._dashboard.activation_requested.connect(self._allow_focus)
        self._trash.changed.connect(lambda: self._dashboard.reload(force=True))
        self._switch_page(0)
        self._restore_geometry(cfg)
        self._snap_side = ""
        self._snap_to_edges()
        # 构造时按配置校正置顶（默认 True，与早期硬编码行为一致）
        self.set_topmost(bool(cfg.get("always_topmost", True)))

    # ------------------------------------------------------------------
    # 标题栏
    # ------------------------------------------------------------------

    def _build_title_bar(self) -> QWidget:
        bar = QWidget(self)
        bar.setFixedHeight(_TITLE_BAR_H)
        bar.setObjectName("TitleBar")

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 0, 8, 0)
        layout.setSpacing(6)

        self._title_label = QLabel("TodoSeine")
        self._title_label.setStyleSheet(
            f"color: {theme.TEXT.name()}; font-size: 13px; font-weight: 600;"
            " background: transparent;")
        layout.addWidget(self._title_label)

        # 导航胶囊组（迷你模式下整组隐藏）
        self._nav_box = QWidget(bar)
        nav_layout = QHBoxLayout(self._nav_box)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(2)

        self._nav: dict = {}
        for text, page in _PAGES:
            nav_btn = QPushButton(text)
            nav_btn.setCheckable(True)
            nav_btn.setFixedHeight(24)
            nav_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            nav_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            nav_btn.setStyleSheet(
                "QPushButton { background: transparent; border: none; border-radius: 12px;"
                f" color: {theme.SUB.name()}; font-size: 12px; padding: 0 8px; }}"
                f"QPushButton:hover {{ background: {theme.CARD_HOVER.name()};"
                f" color: {theme.TEXT.name()}; }}"
                f"QPushButton:checked {{ background: {theme.ACCENT.name()};"
                f" color: {theme.ACCENT_TEXT.name()}; font-weight: 600; }}"
            )
            nav_btn.clicked.connect(lambda _checked=False, p=page: self._switch_page(p))
            self._nav[page] = nav_btn
            nav_layout.addWidget(nav_btn)
        layout.addWidget(self._nav_box)
        layout.addStretch(1)

        # 迷你 / 锁定 / 窗口按钮
        self._mini_btn = QPushButton("⊟")
        self._mini_btn.setToolTip("收起到胶囊")
        self._mini_btn.setFixedSize(28, 26)
        self._style_window_button(self._mini_btn)
        self._mini_btn.clicked.connect(self.toggle_mini)
        layout.addWidget(self._mini_btn)

        # 锁定按钮用 MDL2 图标字体画单色锁形：直接写 🔓 emoji 会被
        # Segoe UI Emoji 渲染成彩色字形，和旁边 ⊟ / — / □ / × 不一致。
        self._lock_btn = QPushButton("\ue785")
        self._lock_btn.setFont(QFont("Segoe MDL2 Assets", 9))
        self._lock_btn.setToolTip("锁定位置和尺寸")

        self._lock_btn.setFixedSize(28, 26)
        self._style_window_button(self._lock_btn)
        self._lock_btn.clicked.connect(self.toggle_lock)
        layout.addWidget(self._lock_btn)

        for text, slot in [
            ("—", self.showMinimized),
            ("□", self._toggle_max),
            ("×", self.close),
        ]:
            btn = QPushButton(text)
            btn.setFixedSize(30, 26)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self._style_window_button(btn)
            btn.clicked.connect(slot)
            if text == "□":
                self._max_btn = btn
            layout.addWidget(btn)

        self._sync_lock_button()
        return bar

    @staticmethod
    def _style_window_button(btn: QPushButton) -> None:
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 4px;"
            f" color: {theme.SUB.name()}; font-size: 12px; }}"
            f"QPushButton:hover {{ background: {theme.CARD_HOVER.name()};"
            f" color: {theme.TEXT.name()}; }}"
        )

    def _build_mini_panel(self) -> QWidget:
        """胶囊模式下的窄条：只显示状态和展开按钮。"""
        panel = QWidget()
        lay = QHBoxLayout(panel)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(8)

        self._mini_label = QLabel("TodoSeine")
        self._mini_label.setStyleSheet(
            f"color: {theme.TEXT.name()}; font-size: 12px; background: transparent;")
        lay.addWidget(self._mini_label, 1)

        expand = QPushButton("展开")
        expand.setFixedHeight(24)
        expand.setCursor(Qt.CursorShape.PointingHandCursor)
        expand.setStyleSheet(
            f"QPushButton {{ background: {theme.CARD_BG.name()}; border: 1px solid"
            f" {theme.CARD_BORDER.name()}; border-radius: 12px; font-size: 11px;"
            f" color: {theme.SUB.name()}; padding: 0 12px; }}"
            f"QPushButton:hover {{ color: {theme.TEXT.name()}; }}"
        )
        expand.clicked.connect(self.toggle_mini)
        lay.addWidget(expand)
        return panel

    # ------------------------------------------------------------------
    # 页切换
    # ------------------------------------------------------------------

    def _switch_page(self, index: int) -> None:
        if not hasattr(self, "_stack"):
            return
        if self._mini:
            self.toggle_mini()
        old_widget = self._stack.currentWidget()
        old_index = self._stack.currentIndex()
        # 切页前截旧页（截完再切，避免抓到已切走的新页）
        old_pix = old_widget.grab() if old_widget is not None else None
        self._stack.setCurrentIndex(index)
        for page, btn in self._nav.items():
            btn.setChecked(page == index)
        if index == 0:
            self._dashboard.reload(force=True)
        elif index == 1:
            self._trash.refresh()
        self.page_changed.emit(index)

        # 圆形扩散转场：从点击的页签按钮中心扩散出圆，揭开新页
        if (old_pix is not None and old_index != index
                and self._anim_effect() != "none"
                and theme.motion_ms(260) > 0):
            btn = self._nav.get(index)
            if btn is not None:
                center = btn.mapTo(self._stack, btn.rect().center())
            else:
                center = self._stack.rect().center()
            overlay = _CircleRevealOverlay(self._stack, old_pix, QPointF(center))
            overlay.start(theme.motion_ms(280))

    def _toggle_max(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def apply_config(self, cfg: dict) -> None:
        """设置保存后由外部调用：看板刷新 + 外观/吸附/锁定/材质即时生效。"""
        self._cfg = cfg
        self._dash_cfg = dict(cfg.get("dash") or {})
        self.set_topmost(bool(cfg.get("always_topmost", True)))
        self._dashboard.reload_config()
        self._trash.set_db_path(str(cfg.get("db_path") or ""))
        self._sync_lock_button()
        self._apply_title_visibility()
        self._apply_no_activate()
        self._apply_exclude_capture()
        # 材质切换：重置标记让 _apply_material 每次都真正执行
        self._material_tried = False
        self._apply_material()
        self.update()

    def notify_new_todos(self, items) -> None:
        """main.py 监听新待办时调用：转发给看板弹 toast + 刷新列表。"""
        self._dashboard.notify_new_todos(items)

    def remind_due(self, pairs) -> None:
        """到期提醒：转发给看板。"""
        self._dashboard.remind_due(pairs)

    # ------------------------------------------------------------------
    # 位置 / 尺寸记忆
    # ------------------------------------------------------------------

    def _restore_geometry(self, cfg: dict) -> None:
        dash = cfg.get("dash") or {}
        width = int(dash.get("width") or 0) or 460
        height = int(dash.get("height") or 0) or 700
        width = max(_MIN_WIDTH, min(_MAX_WIDTH, width))

        x, y = dash.get("x"), dash.get("y")
        screen_idx = dash.get("screen")
        screens = QGuiApplication.screens()

        rect = QRect(int(x), int(y), width, height) if x is not None and y is not None \
            else QRect(0, 0, width, height)

        # 判断窗口是否「可见」：中心点在某块屏内才算。
        # 吸边自动隐藏时窗口只剩 6px 在屏内，intersects() 会误判为在屏上，
        # 导致重启后窗口卡在屏外点不出来。
        def _center_on_screen(r: QRect) -> bool:
            c = r.center()
            for s in QGuiApplication.screens():
                if s.availableGeometry().contains(c):
                    return True
            return False

        # 优先按记忆的屏索引归位：副屏曾断开又重连时，把窗口搬回那块屏
        placed = False
        if (isinstance(screen_idx, int) and 0 <= screen_idx < len(screens)
                and not _center_on_screen(rect)):
            area = screens[screen_idx].availableGeometry()
            rect = QRect(area.right() - width - 24, area.top() + 60,
                         width, min(height, area.height() - 80))
            placed = True

        if not placed and (x is None or not _center_on_screen(rect)):
            screen = QGuiApplication.primaryScreen()
            area = screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)
            rect = QRect(area.right() - width - 24, area.top() + 60, width,
                         min(height, area.height() - 80))

        # 防呆：若保存的位置落在屏幕下半部（用户拖到左下角后忘了拖回），
        # 启动时自动归位到右上角，避免每次都要手动拖。
        if rect.center().y() > (QGuiApplication.primaryScreen().availableGeometry().height() * 0.55
                                if QGuiApplication.primaryScreen() else 500):
            area = (QGuiApplication.primaryScreen().availableGeometry()
                    if QGuiApplication.primaryScreen() else QRect(0, 0, 1280, 800))
            rect = QRect(area.right() - rect.width() - 24, area.top() + 60,
                         rect.width(), min(rect.height(), area.height() - 80))

        # 高 DPI（150% 缩放）下控件按比例变大，固定配置宽度会把内容挤出窗口右侧。
        # 用控件自身的最小建议宽度兜底，避免文字被硬裁掉。
        try:
            needed = self.sizeHint().width()
        except Exception:  # noqa: BLE001
            needed = 0
        if needed > rect.width():
            limit = (QGuiApplication.primaryScreen().availableGeometry().width()
                     if QGuiApplication.primaryScreen() else needed)
            rect.setWidth(min(needed, limit))
        self.setGeometry(self._clamp_to_workarea(rect))

    @staticmethod
    def _intersects_any_screen(rect: QRect) -> bool:
        for screen in QGuiApplication.screens():
            if screen.availableGeometry().intersects(rect):
                return True
        return False

    def _clamp_to_workarea(self, rect: QRect) -> QRect:
        """把窗口矩形夹紧到其所在屏的可用工作区内，避免被任务栏/侧边栏挡住。

        优先保证下边不钻进任务栏（用户最常遇到），再依次保证上/左/右不越界。
        若窗口本身比工作区还大，则按工作区尺寸裁切并贴左上角。
        屏幕按矩形中心点判定，这样在窗口尚未落位（恢复几何时）也能取到正确的屏。
        """
        center = rect.center()
        screen = QGuiApplication.screenAt(center) or self.screen() \
            or QGuiApplication.primaryScreen()
        if screen is None:
            return rect
        area = screen.availableGeometry()
        r = QRect(rect)
        if r.width() > area.width():
            r.setWidth(area.width())
        if r.height() > area.height():
            r.setHeight(area.height())
        # 先保证下边不超出任务栏
        if r.bottom() > area.bottom():
            r.moveTop(area.bottom() - r.height() + 1)
        if r.top() < area.top():
            r.moveTop(area.top())
        if r.right() > area.right():
            r.moveLeft(area.right() - r.width() + 1)
        if r.left() < area.left():
            r.moveLeft(area.left())
        return r

    def _save_geometry(self) -> None:
        if not self.isVisible() or self.isMaximized():
            return
        # 吸边自动隐藏时窗口大部分在屏外，保存这个位置会导致重启后点不出来。
        # 此时保存隐藏前的可见位置（_visible_pos）。
        if self._hidden_edge and self._visible_pos is not None:
            save_x = self._visible_pos.x()
            save_y = self._visible_pos.y()
        else:
            save_x = self.x()
            save_y = self.y()
        try:
            cfg = c2t_config.load()
        except Exception:  # noqa: BLE001
            return
        dash = dict(cfg.get("dash") or {})
        geo = self.geometry()
        # 迷你模式下只保存位置，不覆盖正常模式的宽高（切回看板时用 _mini_saved 还原）
        if self._mini:
            save_w = int(dash.get("width") or geo.width())
            save_h = int(dash.get("height") or geo.height())
        else:
            save_w = geo.width()
            save_h = geo.height()
        # 记下窗口当前所在的屏索引，下次启动可归位到同一块屏
        screens = QGuiApplication.screens()
        cur = QGuiApplication.screenAt(QPoint(save_x + geo.width() // 2,
                                              save_y + geo.height() // 2))
        screen_idx = screens.index(cur) if cur in screens else -1
        dash.update({"x": save_x, "y": save_y,
                     "width": save_w, "height": save_h,
                     "screen": screen_idx})
        cfg["dash"] = dash
        try:
            c2t_config.save(cfg, cfg.get("config_file") or None)
        except Exception:  # noqa: BLE001 - 写配置失败不该影响窗口
            pass

    # ------------------------------------------------------------------
    # 吸附
    # ------------------------------------------------------------------

    def _snap_to_edges(self) -> None:
        if not self._dash_cfg.get("snap", True) or self._mini:
            return
        if self._dash_cfg.get("lock"):
            return
        screen = QGuiApplication.screenAt(self.frameGeometry().center()) \
            or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        geo = self.geometry()
        x, y = geo.x(), geo.y()

        # 吸附阈值可配（DeskBox 的 resizeSnapSpacing 口径，默认 16px）
        try:
            snap_gap = int(self._dash_cfg.get("snap_gap") or _SNAP_PX)
        except (TypeError, ValueError):
            snap_gap = _SNAP_PX
        snap_gap = max(0, min(60, snap_gap))

        self._snap_side = ""
        snapped_left = abs(x - area.left()) <= snap_gap
        snapped_right = abs(area.right() - (x + geo.width())) <= snap_gap
        snapped_top = abs(y - area.top()) <= snap_gap
        snapped_bottom = abs(area.bottom() - (y + geo.height())) <= snap_gap

        if snapped_left:
            x = area.left()
            self._snap_side = "left"
        elif snapped_right:
            x = area.right() - geo.width() + 1
            self._snap_side = "right"
        if snapped_top:
            y = area.top()
            # 角落时优先横向收起（左右条更易抓取），只有纯顶/底边才用纵向
            if not self._snap_side:
                self._snap_side = "top"
        elif snapped_bottom:
            y = area.bottom() - geo.height() + 1
            if not self._snap_side:
                self._snap_side = "bottom"
        if (x, y) != (geo.x(), geo.y()):
            self.move(x, y)

    # ------------------------------------------------------------------
    # 迷你胶囊
    # ------------------------------------------------------------------

    def is_mini(self) -> bool:
        return self._mini

    def toggle_mini(self) -> None:
        anim = bool(self._dash_cfg.get("anim", True))
        if self._mini:
            self._mini = False
            if self._mini_saved is not None:
                target = self._mini_saved
                if anim:
                    # 展开动画期间临时放开最小尺寸，避免起始值被最小宽度钳住跳变
                    self.setMinimumSize(0, 0)
                    self._animate_to_geometry(target)
                else:
                    self.setMinimumSize(_MIN_WIDTH, _MIN_HEIGHT)
                    self.setGeometry(target)
            self._apply_title_visibility()
            self._title_label.setText("TodoSeine")
            self._nav_box.setVisible(True)
            self._lock_btn.setVisible(True)
            self._mini_btn.setText("⊟")
            self._mini_btn.setToolTip("收起到胶囊")
            self._stack.setCurrentIndex(0)
            self._dashboard.reload(force=True)
        else:
            self._mini_saved = self.geometry()
            self._mini = True
            self.setMinimumSize(_MINI_W, _MINI_H)
            self._mini_panel.setVisible(True)
            self._title_label.setText("TodoSeine · 胶囊")
            self._nav_box.setVisible(False)
            self._lock_btn.setVisible(False)
            self._mini_btn.setText("⇱")
            self._mini_btn.setToolTip("展开看板")
            self._stack.setCurrentIndex(5)
            target = QRect(self.pos().x(), self.pos().y(), _MINI_W, _MINI_H)
            if anim:
                self._animate_to_geometry(target)
            else:
                self.setGeometry(target)
        self._geo_timer.start()

    def _on_size_anim_finished(self) -> None:
        """迷你 ↔ 展开动画结束：恢复最小尺寸约束，并把展开后的尺寸钉回原样。

        只恢复最小尺寸是不够的：展开瞬间最小尺寸被临时放到 0，
        布局会按内容 sizeHint 把窗口撑高（实测 700 → 1158），所以这里补一次 setGeometry。
        """
        if self._mini:
            self.setMinimumSize(_MINI_W, _MINI_H)
        else:
            self.setMinimumSize(_MIN_WIDTH, _MIN_HEIGHT)
            if self._mini_saved is not None:
                self.setGeometry(self._clamp_to_workarea(self._mini_saved))

    def _anim_effect(self) -> str:
        """动效类型：slide / fade / none（DashBox widgetAnimationEffect 口径）。"""
        return str(self._dash_cfg.get("anim_effect") or "slide").lower()

    def _animate_to_geometry(self, target: QRect) -> None:
        """位置 + 尺寸并行动画；窗口不可见时直接落位（否则动画永不推进）。"""
        effect = self._anim_effect()
        self._slide_anim.stop()
        self._size_anim.stop()
        if not self.isVisible() or effect == "none":
            self.setGeometry(target)
            self._on_size_anim_finished()
            return
        if effect == "fade":
            # 淡入淡出：位置直接落位，用窗口级 windowOpacity 做过渡。
            # 不用 QGraphicsOpacityEffect —— 那会走离屏合成，
            # 在无边框 + 亚克力窗口上会触发渲染异常。
            self.setGeometry(target)
            self._fade_in()
            return
        self._slide_anim.setStartValue(self.pos())
        self._slide_anim.setEndValue(target.topLeft())
        self._size_anim.setStartValue(self.size())
        self._size_anim.setEndValue(target.size())
        self._slide_anim.setDuration(theme.motion_ms(180))
        self._size_anim.setDuration(theme.motion_ms(220))
        self._slide_anim.start()
        self._size_anim.start()

    def _fade_in(self) -> None:
        """窗口整体不透明度 0 → 1。窗口自身的半透明由 paintEvent 的 alpha 负责，
        这里只做进场过渡，结束后必须回到 1.0，否则会和 paint 叠加成双重透明。"""
        ms = theme.motion_ms(180)
        if ms <= 0:
            self.setWindowOpacity(1.0)
            return
        self._fade_anim.stop()
        self.setWindowOpacity(0.0)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setDuration(ms)
        self._fade_anim.start()

    def set_mini_summary(self, text: str) -> None:
        """监听状态回写胶囊文字（由 main.py 调用）。"""
        self._mini_label.setText(text)

    # ------------------------------------------------------------------
    # 锁定
    # ------------------------------------------------------------------

    def toggle_lock(self) -> None:
        locked = not bool(self._dash_cfg.get("lock"))
        self._dash_cfg["lock"] = locked
        self._sync_lock_button()
        # 立刻落盘，重启后仍锁定
        try:
            cfg = c2t_config.load()
            dash = dict(cfg.get("dash") or {})
            dash["lock"] = locked
            cfg["dash"] = dash
            c2t_config.save(cfg, cfg.get("config_file") or None)
        except Exception:  # noqa: BLE001
            pass

    def _sync_lock_button(self) -> None:
        locked = bool(self._dash_cfg.get("lock"))
        self._lock_btn.setText("\ue72e" if locked else "\ue785")
        self._lock_btn.setToolTip("已锁定，点击解锁" if locked else "锁定位置和尺寸")
        # 锁定瞬间鼠标可能正停在边框上，光标仍是改尺寸样式，这里复位
        self.setCursor(Qt.CursorShape.ArrowCursor)

    # ------------------------------------------------------------------
    # 背景
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        try:
            opacity = float(self._dash_cfg.get("opacity") or 0.94)
        except (TypeError, ValueError):
            opacity = 0.94
        opacity = max(0.5, min(1.0, opacity))
        alpha = 150 if self._material_applied else int(255 * opacity)
        painter.setBrush(QColor(33, 32, 36, alpha))
        radius = float(self._dash_cfg.get("radius") or 12)
        # 边框宽度/颜色可在设置面板调整；宽度为 0 时不画
        try:
            border_w = int(self._dash_cfg.get("border") or 1)
        except (TypeError, ValueError):
            border_w = 1
        border_w = max(0, min(6, border_w))
        border_color = str(self._dash_cfg.get("border_color") or "").strip()
        if not border_color:
            # 边框颜色模式：中性灰 / 跟随强调色（DeskBox 的 widgetBorderColorMode）
            border_color = (theme.ACCENT.name()
                            if self._dash_cfg.get("border_accent")
                            else theme.BORDER.name())
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.drawRoundedRect(rect, radius, radius)
        if border_w > 0:
            pen = QPen(QColor(border_color))
            pen.setWidth(border_w)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # 边框画在圆角矩形的内沿，避免被窗口边缘裁掉一半
            inner = rect.adjusted(border_w / 2.0, border_w / 2.0,
                                  -border_w / 2.0, -border_w / 2.0)
            painter.drawRoundedRect(inner, max(0.0, radius - border_w / 2.0),
                                    max(0.0, radius - border_w / 2.0))
        painter.end()

    # ---------- 不抢焦点 ----------

    def set_topmost(self, on: bool) -> None:
        """切换「常驻最上层」。

        Qt 的 setWindowFlag 会重建原生窗口句柄，必须重新 show() 才能看见，
        而且材质要在新句柄上重新应用，否则亚克力会丢。
        """
        current = bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        if current == bool(on):
            return
        # setWindowFlag 会先把窗口隐藏（重建句柄），所以可见性必须在改之前取，
        # 改完之后再 isVisible() 已经是假的了，窗口就再也回不来。
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(on))
        if was_visible:
            self.show()
        self._material_tried = False
        self._apply_material()

    def _apply_no_activate(self) -> None:
        """点击 / 拖拽 / 勾选时不激活窗口，不把前台窗口顶走。"""
        hwnd = dwm.hwnd_of(self)
        if not hwnd:
            return
        enabled = bool(self._dash_cfg.get("no_activate", True))
        if dwm.set_no_activate(hwnd, enabled):
            self._noactivate_off = False

    def _allow_focus(self) -> None:
        """用户点输入框：临时取消 NOACTIVATE 并激活，失焦后再恢复。"""
        if not self._dash_cfg.get("no_activate", True):
            return
        hwnd = dwm.hwnd_of(self)
        if hwnd and dwm.set_no_activate(hwnd, False):
            self._noactivate_off = True
        self.activateWindow()
        self.raise_()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if (event.type() == QEvent.Type.ActivationChange
                and self._noactivate_off and not self.isActiveWindow()):
            self._apply_no_activate()

    # ---------- 贴边自动收起 ----------

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hide_timer.stop()
        if self._hidden_edge:
            self._slide_back()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        super().leaveEvent(event)
        if (self._hidden_edge or not self._snap_side
                or self._mini or self._dash_cfg.get("lock")
                or not self._dash_cfg.get("auto_hide", True)):
            return
        self._hide_timer.start()

    def _auto_hide(self) -> None:
        if (self._hidden_edge or not self._snap_side or self._mini
                or self._dash_cfg.get("lock")
                or not self._dash_cfg.get("auto_hide", True)):
            return
        if self.underMouse():
            return
        screen = QGuiApplication.screenAt(self.frameGeometry().center()) \
            or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self._visible_pos = self.pos()
        self._hidden_edge = self._snap_side
        target = QPoint(self._visible_pos)
        if self._hidden_edge == "left":
            target.setX(area.left() - self.width() + _AUTO_HIDE_PX)
        elif self._hidden_edge == "right":
            target.setX(area.right() - _AUTO_HIDE_PX + 1)
        elif self._hidden_edge == "top":
            target.setY(area.top() - self.height() + _AUTO_HIDE_PX)
        elif self._hidden_edge == "bottom":
            target.setY(area.bottom() - _AUTO_HIDE_PX + 1)
        self._slide_to(target)

    def reveal(self) -> None:
        """热键唤出时若正收在屏幕边缘，先滑回来。"""
        if self._hidden_edge:
            self._slide_back()

    def _slide_back(self) -> None:
        if self._visible_pos is None:
            self._hidden_edge = ""
            return
        self._slide_to(self._visible_pos)
        self._visible_pos = None
        self._hidden_edge = ""

    def _slide_to(self, target: QPoint) -> None:
        if not bool(self._dash_cfg.get("anim", True)) or self._anim_effect() == "none":
            self.move(target)
            return
        if self._anim_effect() == "fade":
            self.move(target)
            self._fade_in()
            return
        self._slide_anim.stop()
        self._slide_anim.setStartValue(self.pos())
        self._slide_anim.setEndValue(target)
        self._slide_anim.setDuration(theme.motion_ms(180))
        self._slide_anim.start()

    # ---------- 首次显示时应用 DWM 材质 ----------

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._material_tried:
            self._apply_material()
            self._material_tried = True
            self.update()
        self._apply_no_activate()
        self._apply_exclude_capture()

    def _apply_material(self) -> None:
        """按配置里的 material 字符串分发到对应的 DWM / Accent API。

        支持值（dash.material）：
          - "off"      → 关闭系统材质，窗口是自绘深色背景
          - "acrylic"  → Win10/11 都能用的亚克力
          - "mica"     → Win11 22H2+ Mica
          - "mica_alt" → Win11 22H2+ Mica Alt
          - "tabbed"   → Win11 22H2+ Tabbed
          - "blur"     → Win10+ 毛玻璃（BlurBehind）

        旧 bool 配置：True→"acrylic"，False→"off"（由 settings_panel._material_from_raw 提前转好）。
        """
        raw = self._dash_cfg.get("material", "acrylic")
        kind = raw if isinstance(raw, str) else ("acrylic" if raw else "off")

        hwnd = dwm.hwnd_of(self)
        if not hwnd:
            self._material_applied = False
            return

        dwm.set_round_corners(hwnd, bool(self._dash_cfg.get("corner_round", True)))
        dwm.set_immersive_dark(hwnd, True)
        # 无边框窗口默认没有系统阴影，开启 NC 渲染让 DWM 画投影
        dwm.enable_shadow(hwnd, True)

        if kind == "off":
            self._material_applied = False
            # 关掉可能残留的 backdrop（设为 0）
            try:
                import ctypes
                val = ctypes.c_int(0)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 38, ctypes.byref(val), ctypes.sizeof(val))
            except Exception:  # noqa: BLE001
                pass
            return

        try:
            intensity = float(self._dash_cfg.get("material_intensity", 0.65))
        except (TypeError, ValueError):
            intensity = 0.65
        intensity = max(0.0, min(1.0, intensity))
        # tint_bgr 的 alpha 控制材质浓淡：0=几乎全透，1=完全不透
        tint_alpha = round(intensity * 255)

        ok = False
        if kind in ("mica", "mica_alt", "tabbed"):
            backdrop_kind = {"mica": 2, "mica_alt": 3, "tabbed": 4}[kind]
            ok = dwm.set_system_backdrop(hwnd, kind=backdrop_kind)
        elif kind == "blur":
            ok = dwm.set_blur_behind(hwnd)
        elif kind == "acrylic":
            ok = dwm.set_acrylic(
                hwnd, tint_bgr=(tint_alpha << 24) | 0x00292929)
            # 亚克力在新系统上可能失败，fallback 到 Mica Alt
            if not ok:
                ok = dwm.set_system_backdrop(hwnd, kind=3)

        self._material_applied = ok

    def _apply_exclude_capture(self) -> None:
        """按配置决定是否把窗口从截屏 / 录屏里排除（隐私保护）。"""
        hwnd = dwm.hwnd_of(self)
        if not hwnd:
            return
        exclude = bool(self._dash_cfg.get("exclude_from_capture", False))
        dwm.set_exclude_from_capture(hwnd, exclude)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 窄窗口下标题文字让位给导航，避免「关于」被挤掉；胶囊模式始终显示
        if hasattr(self, "_title_label"):
            self._title_label.setVisible(
                self._mini or self.width() >= _COMPACT_W)

    def _apply_title_visibility(self) -> None:
        """外观设置里的「时钟」等开关由看板自己处理，这里只管边框。"""
        if hasattr(self, "_title_label"):
            self._title_label.setVisible(
                self._mini or self.width() >= _COMPACT_W)
        self.update()

    # ------------------------------------------------------------------
    # 无边框但保留阴影
    # ------------------------------------------------------------------

    def nativeEvent(self, event_type, message):  # noqa: N802
        if os.name != "nt":
            return super().nativeEvent(event_type, message)

        if event_type == b"windows_generic_MSG":
            try:
                msg = wintypes.MSG.from_address(int(message))
            except Exception:  # noqa: BLE001
                return super().nativeEvent(event_type, message)

            if msg.message == _WM_NCCALCSIZE:
                if msg.wParam == 0:
                    return True, 0
                try:
                    ncc = ctypes.cast(
                        msg.lParam,
                        ctypes.POINTER(wintypes.NCCALCSIZE_PARAMS),
                    ).contents
                    ncc.rgrc[0].left += 1
                    ncc.rgrc[0].top += 1
                    ncc.rgrc[0].right -= 1
                    ncc.rgrc[0].bottom -= 1
                except Exception:  # noqa: BLE001
                    pass
                return True, 0

            if msg.message == _WM_NCHITTEST:
                # 让最大化按钮区域返回 HTMAXBUTTON，触发 Win11 吸附布局菜单
                result = self._hit_test_max_button(msg.lParam)
                if result is not None:
                    return True, result

        return super().nativeEvent(event_type, message)

    def _hit_test_max_button(self, lparam) -> Optional[int]:
        """判断鼠标是否在最大化按钮上，是则返回 HTMAXBUTTON。

        lParam 低字是屏幕 x，高字是屏幕 y。需要转成客户区坐标再和
        最大化按钮的几何比对。命中返回 _HTMAXBUTTON，未命中返回 None
        （交给默认处理）。
        """
        if not hasattr(self, "_max_btn") or self._max_btn is None:
            return None
        x = lparam & 0xFFFF
        y = (lparam >> 16) & 0xFFFF
        if x >= 0x8000:
            x -= 0x10000
        if y >= 0x8000:
            y -= 0x10000
        pos = self.mapFromGlobal(QPoint(x, y))
        # 最大化按钮在标题栏里，先判断是否落在标题栏高度内
        if pos.y() < 0 or pos.y() > _TITLE_BAR_H:
            return None
        btn = self._max_btn
        # btn.geometry() 是相对于父（标题栏）的坐标，标题栏在窗口顶部 y=0
        btn_rect = btn.geometry()
        # 标题栏本身可能有 margins，用 btn 的 mapTo 转换到窗口坐标
        top_left = btn.mapTo(self, QPoint(0, 0))
        btn_rect_in_window = QRect(top_left.x(), top_left.y(),
                                   btn_rect.width(), btn_rect.height())
        if btn_rect_in_window.contains(pos):
            return _HTMAXBUTTON
        return None

    # ------------------------------------------------------------------
    # 拖拽移动 / 拖边改尺寸
    # ------------------------------------------------------------------

    def _edge_at(self, pos: QPoint) -> str:
        """返回鼠标所在的可拖拽区域。

        取值："" / left / right / top / bottom / top-left / top-right /
        bottom-left / bottom-right。四角优先于四边，和系统窗口行为一致。
        """
        if self._mini or self._dash_cfg.get("lock"):
            return ""
        w, h = self.width(), self.height()
        x, y = pos.x(), pos.y()
        near_left = x <= _EDGE_MARGIN
        near_right = x >= w - _EDGE_MARGIN
        near_top = y <= _EDGE_MARGIN
        near_bottom = y >= h - _EDGE_MARGIN
        if near_top and near_left:
            return "top-left"
        if near_top and near_right:
            return "top-right"
        if near_bottom and near_left:
            return "bottom-left"
        if near_bottom and near_right:
            return "bottom-right"
        if near_left:
            return "left"
        if near_right:
            return "right"
        if near_top:
            return "top"
        if near_bottom:
            return "bottom"
        return ""

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            # 窗口已自动收起（吸到屏幕边缘、只剩几像素露在外）时，
            # 标题栏已滑出屏幕，用户点不到拖拽区。
            # 此时按下鼠标：先立即滑回可见位置，再从按下点开始拖拽。
            if self._hidden_edge:
                self._hide_timer.stop()
                self._slide_anim.stop()
                if self._visible_pos is not None:
                    self.move(self._visible_pos)
                self._visible_pos = None
                self._hidden_edge = ""
                self._drag_pos = (
                    event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                )
                event.accept()
                return
            local_pos = event.position().toPoint()
            edge = self._edge_at(local_pos)
            if edge:
                self._resize_edge = edge
                self._resize_start = (
                    event.globalPosition().toPoint(),
                    self.geometry(),
                )
                event.accept()
                return
            if (self._title_bar.geometry().contains(local_pos)
                    and not self._dash_cfg.get("lock")):
                self._drag_pos = (
                    event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                )
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._resize_edge and event.buttons() & Qt.MouseButton.LeftButton:
            self._do_resize(event.globalPosition().toPoint())
            event.accept()
            return
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        if not event.buttons():
            self.setCursor(
                _CURSOR_FOR_EDGE.get(self._edge_at(event.position().toPoint()),
                                     Qt.CursorShape.ArrowCursor))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._resize_edge:
            self._resize_edge = ""
            self._resize_start = None
            self._snap_to_edges()
            self._geo_timer.start()
            event.accept()
            return
        if self._drag_pos is not None:
            self._drag_pos = None
            self._snap_to_edges()
            self._geo_timer.start()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _do_resize(self, global_pos: QPoint) -> None:
        """按拖拽方向同时调整位置和尺寸（八方向）。

        左 / 上边拖拽时窗口原点跟着移动，右 / 下边只改宽高。
        高度上限取当前屏幕可用高度，避免拉到屏幕外。
        """
        if not self._resize_start:
            return
        start_pos, start_geo = self._resize_start
        edge = self._resize_edge
        dx = global_pos.x() - start_pos.x()
        dy = global_pos.y() - start_pos.y()

        screen = self.screen()
        max_h = (max(_MIN_HEIGHT, screen.availableGeometry().height())
                 if screen is not None else _MIN_HEIGHT * 8)

        left, top = start_geo.x(), start_geo.y()
        width, height = start_geo.width(), start_geo.height()

        if "left" in edge:
            width = max(_MIN_WIDTH, min(_MAX_WIDTH, width - dx))
            left = start_geo.x() + (start_geo.width() - width)
        elif "right" in edge:
            width = max(_MIN_WIDTH, min(_MAX_WIDTH, width + dx))

        if "top" in edge:
            height = max(_MIN_HEIGHT, min(max_h, height - dy))
            top = start_geo.y() + (start_geo.height() - height)
        elif "bottom" in edge:
            height = max(_MIN_HEIGHT, min(max_h, height + dy))

        self.setGeometry(self._clamp_to_workarea(QRect(left, top, width, height)))

    # ------------------------------------------------------------------
    # 关闭：不是真关，是最小化到托盘
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self._save_geometry()
        self.hide()
        self.minimized_to_tray.emit()
