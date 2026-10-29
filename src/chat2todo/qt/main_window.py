"""PySide6 无边框 QMainWindow —— DeskBox 风格主窗口壳。

职责：
  - 无边框 + DWM 圆角 + Mica/Acrylic 材质
  - PerMonitorV2 高 DPI
  - nativeEvent 处理 WM_NCCALCSIZE（无边框但保留阴影）
  - 标题栏拖拽移动 / 左右拖边改宽 / 屏幕边缘吸附 / 锁定位置
  - 位置尺寸记忆（写回配置）
  - 迷你胶囊模式（只留一条状态条）
  - 最小化到托盘

内容区五个页签：待办 / 回收站 / 统计 / 设置 / 关于。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Optional

from PySide6.QtCore import (QEasingCurve, QEvent, QPoint, QPropertyAnimation,
                            QRect, QRectF, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent, QPainter, QPen
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

_EDGE_MARGIN = 7       # 边缘拖拽改宽的像素阈值
_MIN_WIDTH = 460
_MIN_HEIGHT = 420
_MAX_WIDTH = 980
_TITLE_BAR_H = 36
_SNAP_PX = 16          # 距屏幕边缘多少像素内自动吸附
_COMPACT_W = 560       # 窄于这个宽度就藏掉标题文字，把空间让给导航

_MINI_W = 300
_MINI_H = 96

# 贴边自动收起后仍留在屏幕内的像素宽度（鼠标移上去再滑出）
_AUTO_HIDE_PX = 6
_AUTO_HIDE_DELAY = 450

# (页签文字, 页索引)
_PAGES = (("待办", 0), ("回收站", 1), ("统计", 2), ("设置", 3), ("关于", 4))


class FramelessMainWindow(QMainWindow):
    """无边框主窗口壳。"""

    minimized_to_tray = Signal()
    closing = Signal()
    page_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        cfg = c2t_config.load()
        self._cfg = cfg
        self._dash_cfg = dict(cfg.get("dash") or {})

        self.setWindowTitle("Chat2Todo")
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
        self._snap_side = ""              # "left" / "right" / ""
        self._hidden_edge = ""            # 已收起时停在哪个边
        self._visible_pos: Optional[QPoint] = None
        self._noactivate_off = False      # 用户点输入框时临时让出焦点
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(_AUTO_HIDE_DELAY)
        self._hide_timer.timeout.connect(self._auto_hide)
        self._slide_anim = QPropertyAnimation(self, b"pos", self)
        self._slide_anim.setDuration(180)
        self._slide_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        # 迷你 ↔ 展开时尺寸同步过渡（和位置动画并行）
        self._size_anim = QPropertyAnimation(self, b"size", self)
        self._size_anim.setDuration(220)
        self._size_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._size_anim.finished.connect(self._on_size_anim_finished)

        # ---------- 中心 Widget ----------
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._title_bar = self._build_title_bar()
        root.addWidget(self._title_bar)

        from .dashboard import Dashboard
        from .pages import AboutPage, StatsPage, TrashPage
        from .settings_panel import SettingsPanel

        self._stack = QStackedWidget(central)
        self._dashboard = Dashboard(parent=central)
        self._trash = TrashPage(str(cfg.get("db_path") or ""), central)
        self._stats = StatsPage(str(cfg.get("db_path") or ""), central)
        self._settings = SettingsPanel(central)
        self._about = AboutPage(str(cfg.get("db_path") or ""), central)
        self._mini_panel = self._build_mini_panel()

        for widget in (self._dashboard, self._trash, self._stats,
                       self._settings, self._about, self._mini_panel):
            self._stack.addWidget(widget)
        root.addWidget(self._stack, 1)

        self._dashboard.settings_requested.connect(lambda: self._switch_page(3))
        self._dashboard.activation_requested.connect(self._allow_focus)
        self._settings.config_saved.connect(self._on_config_saved)
        self._trash.changed.connect(lambda: self._dashboard.reload(force=True))
        self._switch_page(0)
        self._restore_geometry(cfg)
        self._snap_side = ""
        self._snap_to_edges()

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

        self._title_label = QLabel("Chat2Todo")
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

        self._lock_btn = QPushButton("🔓")
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

        self._mini_label = QLabel("Chat2Todo")
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
        self._stack.setCurrentIndex(index)
        for page, btn in self._nav.items():
            btn.setChecked(page == index)
        if index == 0:
            self._dashboard.reload(force=True)
        elif index == 1:
            self._trash.refresh()
        elif index == 2:
            self._stats.refresh()
        self.page_changed.emit(index)

    def _toggle_max(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _on_config_saved(self, cfg: dict) -> None:
        """设置页保存：看板刷新 + 外观/吸附/锁定即时生效。"""
        self._cfg = cfg
        self._dash_cfg = dict(cfg.get("dash") or {})
        self._dashboard.reload_config()
        for page in (self._trash, self._stats, self._about):
            page.set_db_path(str(cfg.get("db_path") or ""))
        self._sync_lock_button()
        self._apply_title_visibility()
        self._apply_no_activate()
        self._apply_exclude_capture()
        self.update()

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

        # 优先按记忆的屏索引归位：副屏曾断开又重连时，把窗口搬回那块屏
        placed = False
        if (isinstance(screen_idx, int) and 0 <= screen_idx < len(screens)
                and not self._intersects_any_screen(rect)):
            area = screens[screen_idx].availableGeometry()
            rect = QRect(area.right() - width - 24, area.top() + 60,
                         width, min(height, area.height() - 80))
            placed = True

        if not placed and (x is None or not self._intersects_any_screen(rect)):
            screen = QGuiApplication.primaryScreen()
            area = screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)
            rect = QRect(area.right() - width - 24, area.top() + 60, width,
                         min(height, area.height() - 80))

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
        self.setGeometry(rect)

    @staticmethod
    def _intersects_any_screen(rect: QRect) -> bool:
        for screen in QGuiApplication.screens():
            if screen.availableGeometry().intersects(rect):
                return True
        return False

    def _save_geometry(self) -> None:
        if self._mini or not self.isVisible() or self.isMaximized():
            return
        try:
            cfg = c2t_config.load()
        except Exception:  # noqa: BLE001
            return
        dash = dict(cfg.get("dash") or {})
        geo = self.geometry()
        # 记下窗口当前所在的屏索引，下次启动可归位到同一块屏
        screens = QGuiApplication.screens()
        cur = QGuiApplication.screenAt(geo.center())
        screen_idx = screens.index(cur) if cur in screens else -1
        dash.update({"x": geo.x(), "y": geo.y(),
                     "width": geo.width(), "height": geo.height(),
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

        self._snap_side = ""
        if abs(x - area.left()) <= _SNAP_PX:
            x = area.left()
            self._snap_side = "left"
        elif abs(area.right() - (x + geo.width())) <= _SNAP_PX:
            x = area.right() - geo.width() + 1
            self._snap_side = "right"
        if abs(y - area.top()) <= _SNAP_PX:
            y = area.top()
        elif abs(area.bottom() - (y + geo.height())) <= _SNAP_PX:
            y = area.bottom() - geo.height() + 1
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
            self._title_label.setText("Chat2Todo")
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
            self._title_label.setText("Chat2Todo · 胶囊")
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
                self.setGeometry(self._mini_saved)

    def _animate_to_geometry(self, target: QRect) -> None:
        """位置 + 尺寸并行动画；窗口不可见时直接落位（否则动画永不推进）。"""
        self._slide_anim.stop()
        self._size_anim.stop()
        if not self.isVisible():
            self.setGeometry(target)
            self._on_size_anim_finished()
            return
        self._slide_anim.setStartValue(self.pos())
        self._slide_anim.setEndValue(target.topLeft())
        self._size_anim.setStartValue(self.size())
        self._size_anim.setEndValue(target.size())
        self._slide_anim.start()
        self._size_anim.start()

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
        self._lock_btn.setText("🔒" if locked else "🔓")
        self._lock_btn.setToolTip("已锁定，点击解锁" if locked else "锁定位置和尺寸")
        self.setCursor(Qt.CursorShape.ArrowCursor if locked
                       else Qt.CursorShape.ArrowCursor)

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
            border_color = theme.BORDER.name()
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
        else:
            target.setX(area.right() - _AUTO_HIDE_PX + 1)
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
        if not bool(self._dash_cfg.get("anim", True)):
            self.move(target)
            return
        self._slide_anim.stop()
        self._slide_anim.setStartValue(self.pos())
        self._slide_anim.setEndValue(target)
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
        if not self._dash_cfg.get("material", True):
            self._material_applied = False
            return
        hwnd = dwm.hwnd_of(self)
        if not hwnd:
            return
        dwm.set_round_corners(hwnd)
        dwm.set_immersive_dark(hwnd, True)
        # 无边框窗口默认没有系统阴影，开启 NC 渲染让 DWM 画投影
        dwm.enable_shadow(hwnd, True)
        ok = dwm.set_system_backdrop(hwnd, kind=3)
        if not ok:
            ok = dwm.set_acrylic(hwnd, tint_bgr=0x80292929)
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
    # 拖拽移动 / 拖边改宽
    # ------------------------------------------------------------------

    def _edge_at(self, pos: QPoint) -> str:
        if self._mini or self._dash_cfg.get("lock"):
            return ""
        if pos.y() < _TITLE_BAR_H - 8:
            return ""          # 顶部留给拖拽移动
        if pos.x() <= _EDGE_MARGIN:
            return "left"
        if pos.x() >= self.width() - _EDGE_MARGIN:
            return "right"
        return ""

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
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
                    and not self._dash_cfg.get("lock") and not self._mini):
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
                Qt.CursorShape.SizeHorCursor if self._edge_at(event.position().toPoint())
                else Qt.CursorShape.ArrowCursor)
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
        if not self._resize_start:
            return
        start_pos, start_geo = self._resize_start
        dx = global_pos.x() - start_pos.x()
        if self._resize_edge == "right":
            width = max(_MIN_WIDTH, min(_MAX_WIDTH, start_geo.width() + dx))
            self.resize(width, start_geo.height())
        elif self._resize_edge == "left":
            width = max(_MIN_WIDTH, min(_MAX_WIDTH, start_geo.width() - dx))
            new_x = start_geo.x() + (start_geo.width() - width)
            self.setGeometry(new_x, start_geo.y(), width, start_geo.height())

    # ------------------------------------------------------------------
    # 关闭：不是真关，是最小化到托盘
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self._save_geometry()
        self.hide()
        self.minimized_to_tray.emit()
