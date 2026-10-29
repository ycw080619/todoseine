"""看板主体（PySide6 DeskBox 风格），替换原 Tkinter 版 dashboard/app.py 的界面层。

信息架构与 Tk 版一一对应：
  标题栏（≡ / 待办 / 时钟 / 设置）
  → 顶部输入卡（内联添加 + 搜索）
  → 分段筛选（全部 / 重点 / 今日 / 待定 / 完成，带数量）
  → 待办卡片列表（勾选、强调色竖条、到期 chip、改 / ⋯ 菜单、双击看原文）
  → 页脚（统计 + 展开 + 清除已完成）

数据仍走 core.Store 与 dashboard.data，界面只读，勾选 / 编辑时才写回。
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import List, Optional

from PySide6.QtCore import (QEasingCurve, QEvent, QPoint, QPropertyAnimation,
                            QRectF, Qt, QThread, QTimer, Signal)
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QMenu, QMessageBox, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from .. import config as c2t_config
from ..core import Store
from ..core.deadline import label as due_label
from ..core.deadline import parse as due_parse
from ..core.privacy import redact_for_display
from ..dashboard.data import _is_today_due, filter_items, load_items
from ..dashboard.files import _extract_filenames, _find_downloaded_file, _fmt_ts
from ..models import TodoItem
from .widgets import theme
from .widgets.card import CheckDot, RoundedCard
from .widgets.pill_tabs import PillTabs
from .widgets.text import ElidedLabel
from .widgets.toast import Toast

logger = logging.getLogger("chat2todo.qt.dashboard")

TABS = [("todo", "全部"), ("star", "重点"), ("today", "今日"),
        ("maybe", "待定"), ("done", "完成")]

_NO_DB_HINT = "还没有数据：先在「设置」里检查数据库路径，或运行一次同步"


class SmoothScrollArea(QScrollArea):
    """滚轮滚动用 OutCubic 缓动平滑到目标值，连续滚动打断上一段。

    `anim_enabled=False` 时退化为默认瞬时滚动，保证性能敏感场景不受影响。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._anim = QPropertyAnimation(
            self.verticalScrollBar(), b"value", self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setDuration(180)
        self._anim.finished.connect(self._on_anim_finished)
        self._target = 0
        self.anim_enabled = True

    def _on_anim_finished(self) -> None:
        self._target = self.verticalScrollBar().value()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self.anim_enabled:
            super().wheelEvent(event)
            return
        bar = self.verticalScrollBar()
        # 累计到目标值：动画进行中也续上，避免连续滚动丢步
        delta = event.angleDelta().y()
        self._target -= int(delta * bar.singleStep() / 120)
        self._target = max(bar.minimum(), min(bar.maximum(), self._target))
        self._anim.stop()
        self._anim.setStartValue(bar.value())
        self._anim.setEndValue(self._target)
        self._anim.start()


class _LocateWorker(QThread):
    """在后台扫下载目录找文件，避免右键一下界面冻住几秒。

    不挂 parent：交给看板自己持有引用，finished 时 deleteLater 收尾。
    """

    found = Signal(str)

    def __init__(self, names: List[str]):
        super().__init__()
        self._names = names

    def run(self) -> None:  # noqa: D401 - QThread 约定的入口
        try:
            self.found.emit(_find_downloaded_file(self._names) or "")
        except Exception as exc:  # noqa: BLE001 - 扫目录失败按「没找到」处理
            logger.debug("定位下载文件失败：%s", exc)
            self.found.emit("")


def _flat_button(text: str, width: int = 0) -> QPushButton:
    """行内小按钮：透明底，hover 时文字变亮（三级状态的轻量版）。"""
    btn = QPushButton(text)
    if width:
        btn.setFixedWidth(width)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    btn.setStyleSheet(
        f"QPushButton {{ background: transparent; border: none;"
        f" color: {theme.SUB.name()}; font-size: 11px; padding: 0 4px; }}"
        f"QPushButton:hover {{ color: {theme.TEXT.name()}; }}"
        f"QPushButton:pressed {{ color: {theme.CAPTION.name()}; }}"
    )
    return btn


def _pill_button(text: str, *, accent: bool = False) -> QPushButton:
    """页脚胶囊按钮：常态 → hover → pressed 三档。"""
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    if accent:
        style = (
            f"QPushButton {{ background: {theme.ACCENT.name()};"
            f" color: {theme.ACCENT_TEXT.name()}; border: none; border-radius: 11px;"
            " font-size: 11px; padding: 0 12px; }"
            f"QPushButton:hover {{ background: {theme.mix(theme.ACCENT, theme.PANEL, 0.15).name()}; }}"
            f"QPushButton:pressed {{ background: {theme.mix(theme.ACCENT, theme.PANEL, 0.3).name()}; }}"
        )
    else:
        style = (
            f"QPushButton {{ background: {theme.CARD_BG.name()};"
            f" color: {theme.SUB.name()}; border: 1px solid {theme.CARD_BORDER.name()};"
            " border-radius: 11px; font-size: 11px; padding: 0 12px; }"
            f"QPushButton:hover {{ background: {theme.CARD_HOVER.name()};"
            f" color: {theme.TEXT.name()}; }}"
            f"QPushButton:pressed {{ background: {theme.CARD_PRESSED.name()}; }}"
        )
    btn.setStyleSheet(style)
    btn.setFixedHeight(22)
    return btn


class TodoRow(RoundedCard):
    """单条待办卡片。"""

    toggled = Signal(object)          # 勾选 / 取消勾选
    edited = Signal(object)           # 点「改」
    detail_requested = Signal(object)  # 双击看原文
    menu_requested = Signal(object, object)  # item, 全局坐标

    HEIGHT = 62

    def __init__(self, item: TodoItem, index: int, parent: QWidget | None = None,
                 display_text: Optional[str] = None):
        super().__init__(parent, radius=14)
        self.item = item
        # 列表里显示的是脱敏文本；item.text 仍是原文，留给编辑 / 看原文时用
        self.display_text = (item.text or "") if display_text is None else display_text
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._press_in_dot = False

        star = "★重点" in (item.reason or "")
        due = due_parse(item.text)
        chip = due_label(due)
        now_ts = time.time()

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 10, 8)
        row.setSpacing(14)

        self.dot = CheckDot(self)
        self.dot.setChecked(item.status == "done", animated=False)
        row.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)

        title = ElidedLabel(("★ " if star else "") + self.display_text)
        title_font = QFont(theme.FONT_FAMILY)
        title_font.setPointSize(10)
        if item.status == "done":
            title_font.setStrikeOut(True)
        title.setFont(title_font)
        title.setTextColor(theme.SUB if item.status != "pending" else theme.TEXT)
        title.setStyleSheet("background: transparent;")
        col.addWidget(title)

        bits = []
        if item.status == "unconfirmed":
            bits.append("待确认")
        elif item.status == "done":
            bits.append("已完成")
        else:
            bits.append("规则判定")
        if chip:
            bits.append(chip)
        if item.sender:
            bits.append(item.sender)
        meta = ElidedLabel(" · ".join(bits))
        meta_font = QFont(theme.FONT_FAMILY)
        meta_font.setPointSize(8)
        meta.setFont(meta_font)
        if due and due < now_ts:
            meta_color = theme.WARN
        elif chip.startswith("今天"):
            meta_color = theme.WARN
        elif due and due - now_ts <= 86400:
            meta_color = QColor("#ff9f43")
        else:
            meta_color = theme.SUB
        meta.setTextColor(meta_color)
        meta.setStyleSheet("background: transparent;")
        col.addWidget(meta)
        row.addLayout(col, 1)

        self.edit_btn = _flat_button("改", 24)
        self.edit_btn.clicked.connect(lambda: self.edited.emit(self.item))
        row.addWidget(self.edit_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.more_btn = _flat_button("⋯", 22)
        self.more_btn.clicked.connect(self._open_menu)
        row.addWidget(self.more_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        # 强调色竖条：完成=灰、待确认=黄、其余按序号轮换
        if item.status == "done":
            self.bar_color = QColor("#6f6a64")
        elif item.status == "unconfirmed":
            self.bar_color = theme.WARN
        else:
            self.bar_color = theme.ACCENTS[index % len(theme.ACCENTS)]

    # ---------- 绘制 ----------

    def paint_extra(self, painter: QPainter) -> None:
        bar = QRectF(36.0, 15.0, 3.0, self.height() - 30.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.bar_color)
        painter.drawRoundedRect(bar, 1.5, 1.5)
        self.paint_focus_ring(painter)

    # ---------- 交互 ----------

    def _open_menu(self) -> None:
        pos = self.more_btn.mapToGlobal(QPoint(0, self.more_btn.height()))
        self.menu_requested.emit(self.item, pos)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_in_dot = self.dot.geometry().contains(event.position().toPoint())
            self._animate_to(2.0)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._animate_to(1.0 if self.underMouse() else 0.0)
        if event.button() == Qt.MouseButton.LeftButton and self._press_in_dot:
            self._press_in_dot = False
            if self.rect().contains(event.position().toPoint()):
                self.toggled.emit(self.item)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.detail_requested.emit(self.item)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self.menu_requested.emit(self.item, event.globalPos())

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Space:
            self.toggled.emit(self.item)
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.detail_requested.emit(self.item)
            return
        if event.key() == Qt.Key.Key_Delete:
            self.menu_requested.emit(self.item, self.mapToGlobal(QPoint(0, self.height())))
            return
        super().keyPressEvent(event)


class Dashboard(QWidget):
    """看板主界面。"""

    settings_requested = Signal()
    activation_requested = Signal()   # 用户点输入框：需要主窗口临时让出前台焦点
    data_changed = Signal()           # 看板写库成功：桌面联动 / Obsidian 据此防抖导出

    REFRESH_MS = 3000

    def __init__(self, db_path: Optional[str] = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        cfg = c2t_config.load()
        self._cfg = cfg                      # 隐私脱敏要读 privacy 段，缓存一份
        self._db_path = db_path or cfg.get("db_path") or ""
        dash = cfg.get("dash") or {}
        try:
            self._top_n = max(0, int(dash.get("top_n", 5) or 0))
        except (TypeError, ValueError):
            self._top_n = 5

        self._tab = "todo"
        self._query = ""
        self._top_n_full = False
        self._all: List[TodoItem] = []
        self._rows: List[TodoRow] = []
        self._mtime: Optional[float] = None
        self._day = time.strftime("%Y-%m-%d")
        self._error = ""
        self._locate_worker: Optional[_LocateWorker] = None

        self._build()
        self._scroll.anim_enabled = bool(dash.get("anim", True))
        self._start_timers()
        self.reload(force=True)

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 8, 16, 12)
        root.setSpacing(10)

        # ---- 顶部状态行：时钟 + 设置入口 ----
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addStretch(1)

        self._clock = QLabel("")
        self._clock.setStyleSheet(
            f"color: {theme.SUB.name()}; font-family: Consolas, monospace;"
            " font-size: 12px;"
        )
        header.addWidget(self._clock)

        gear = _flat_button("⚙", 20)
        gear.setToolTip("设置")
        gear.clicked.connect(self.settings_requested.emit)
        header.addWidget(gear)
        root.addLayout(header)

        # ---- 输入卡：添加 + 搜索 ----
        input_card = RoundedCard(self, radius=12, animated=False)
        input_card.setFixedHeight(40)
        in_row = QHBoxLayout(input_card)
        in_row.setContentsMargins(12, 5, 8, 5)
        in_row.setSpacing(6)

        plus = QLabel("＋")
        plus.setStyleSheet(f"color: {theme.SUB.name()}; font-size: 13px;")
        in_row.addWidget(plus)

        self._add_edit = QLineEdit()
        self._add_edit.setPlaceholderText("添加待办，回车入库")
        self._add_edit.returnPressed.connect(self._on_add)
        in_row.addWidget(self._add_edit, 1)

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("搜索")
        self._search_edit.setFixedWidth(84)
        self._search_edit.textChanged.connect(self._on_search)
        in_row.addWidget(self._search_edit)

        _line_style = (
            "QLineEdit { background: transparent; border: none;"
            f" color: {theme.TEXT.name()}; font-size: 12px;"
            " selection-background-color: rgba(76,194,255,80); }"
        )
        self._add_edit.setStyleSheet(_line_style)
        self._search_edit.setStyleSheet(
            _line_style
            + "QLineEdit { border-left: 1px solid rgba(255,255,255,25);"
            " padding-left: 8px; }"
        )
        root.addWidget(input_card)

        # 看板默认不抢焦点（WS_EX_NOACTIVATE），点输入框时才临时让出前台
        for edit in (self._add_edit, self._search_edit):
            edit.installEventFilter(self)

        # ---- 分段筛选 ----
        self._tabs = PillTabs(TABS, self)
        self._tabs.changed.connect(self._on_tab_changed)
        root.addWidget(self._tabs)

        # ---- 列表 ----
        self._scroll = SmoothScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 8px; margin: 2px 0; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,40);"
            " border-radius: 4px; min-height: 24px; }"
            "QScrollBar::handle:vertical:hover { background: rgba(255,255,255,70); }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {"
            " background: transparent; }"
        )
        self._list_host = QWidget()
        self._list_host.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 4, 0)
        self._list_layout.setSpacing(8)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(self._list_host)
        root.addWidget(self._scroll, 1)

        # ---- 页脚 ----
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self._stats = QLabel("")
        self._stats.setStyleSheet(
            f"color: {theme.SUB.name()}; font-size: 11px;"
        )
        footer.addWidget(self._stats)
        footer.addStretch(1)

        self._expand_btn = _pill_button("", accent=False)
        self._expand_btn.hide()
        self._expand_btn.clicked.connect(self._toggle_top_n)
        footer.addWidget(self._expand_btn)

        self._clear_btn = _pill_button("清除已完成")
        self._clear_btn.clicked.connect(self._clear_done)
        footer.addWidget(self._clear_btn)
        root.addLayout(footer)

        QShortcut(QKeySequence.StandardKey.Find, self,
                  activated=self._search_edit.setFocus)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """点输入框时请求主窗口临时取消 NOACTIVATE，否则打不了字。"""
        if event.type() == QEvent.Type.MouseButtonPress and obj in (
                self._add_edit, self._search_edit):
            self.activation_requested.emit()
            edit = obj
            QTimer.singleShot(0, edit.setFocus)
        return super().eventFilter(obj, event)

    def _start_timers(self) -> None:
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(self.REFRESH_MS)
        self._refresh_timer.timeout.connect(self.reload)
        self._refresh_timer.start()

        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start()
        self._tick_clock()

    def _tick_clock(self) -> None:
        self._clock.setText(time.strftime("%H:%M"))
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self.reload(force=True)

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------

    def reload_config(self) -> None:
        """设置页保存后调用：数据库路径 / 显示条数可能变了，重新读取。"""
        cfg = c2t_config.load()
        self._cfg = cfg
        new_path = cfg.get("db_path") or self._db_path
        if new_path != self._db_path:
            self._db_path = new_path
            self._mtime = None
        dash = cfg.get("dash") or {}
        try:
            self._top_n = max(0, int(dash.get("top_n", self._top_n) or 0))
        except (TypeError, ValueError):
            pass
        self._clock_timer.blockSignals(not bool(dash.get("clock", True)))
        self._clock.setVisible(bool(dash.get("clock", True)))
        self._scroll.anim_enabled = bool(dash.get("anim", True))
        self.reload(force=True)

    def notify_new_todos(self, items: List[TodoItem]) -> None:
        """监听线程入库新待办后调用：刷新列表并弹一条轻提示。"""
        self.reload(force=True)
        if not items:
            return
        first = redact_for_display((items[0].text or "").replace("\n", " "),
                                   self._cfg, items[0].session or "")
        text = (f"新增 {len(items)} 条待办：{first[:40]}"
                if len(items) > 1 else f"新待办：{first[:48]}")
        Toast.show_toast(self, text, "查看", self._focus_todo_tab, 4000)

    def _focus_todo_tab(self) -> None:
        self._tab = "todo"
        self._tabs.set_current("todo")
        self._render()

    def remind_due(self, pairs) -> None:
        """到期提醒：pairs = [(TodoItem, due_ts), ...]，只弹一条汇总。"""
        if not pairs:
            return
        item, due_ts = pairs[0]
        text = redact_for_display((item.text or "").replace("\n", " "),
                                  self._cfg, item.session or "")
        if len(pairs) == 1:
            msg = f"⏰ {due_label(due_ts) or '即将到期'}：{text[:40]}"
        else:
            msg = f"⏰ {len(pairs)} 条待办即将到期：{text[:32]}"
        Toast.show_toast(self, msg, "查看", self._focus_todo_tab, 6000)

    def _db_mtime(self) -> Optional[float]:
        try:
            return os.stat(self._db_path).st_mtime
        except OSError:
            return None

    def reload(self, force: bool = False) -> None:
        mtime = self._db_mtime()
        if not force and mtime == self._mtime:
            return
        self._mtime = mtime
        if not self._db_path:
            self._all = []
            self._error = _NO_DB_HINT
            self._render()
            return
        try:
            self._all = load_items(self._db_path)
            self._error = ""
        except Exception as exc:  # noqa: BLE001 - 界面不能因为读库失败而崩
            logger.warning("看板读库失败：%s", exc)
            self._all = []
            self._error = f"读取失败：{exc}"
        self._render()

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def _items_for_tab(self) -> List[TodoItem]:
        if self._tab == "today":
            return [i for i in self._all if _is_today_due(i)]
        if self._tab == "star":
            return [i for i in self._all if "★重点" in (i.reason or "")]
        if self._tab == "maybe":
            return [i for i in self._all if i.status == "unconfirmed"]
        if self._tab == "done":
            try:
                return load_items(self._db_path, "done")[:30]
            except Exception:  # noqa: BLE001
                return []
        return list(self._all)

    def _render(self) -> None:
        tab_items = self._items_for_tab()
        tab_len = len(tab_items)

        if self._query:
            tab_items = filter_items(tab_items, self._query)

        hidden = 0
        if self._tab == "todo" and self._top_n and not self._top_n_full:
            if len(tab_items) > self._top_n:
                hidden = len(tab_items) - self._top_n
                tab_items = sorted(
                    tab_items, key=lambda i: (-i.confidence, i.status != "pending")
                )[:self._top_n]

        open_count = sum(1 for i in self._all if i.status != "done")
        self._tabs.set_counts({
            "todo": tab_len,
            "maybe": sum(1 for i in self._all if i.status == "unconfirmed"),
            "star": sum(1 for i in self._all if "★重点" in (i.reason or "")),
            "today": sum(1 for i in self._all if _is_today_due(i)),
            "done": None,
        })

        self._fill_list(tab_items, hidden)
        self._update_footer(open_count)

    def _clear_rows(self) -> None:
        for row in self._rows:
            self._list_layout.removeWidget(row)
            row.deleteLater()
        self._rows = []

    def _fill_list(self, items: List[TodoItem], hidden: int) -> None:
        self._clear_rows()
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if self._error:
            self._list_layout.insertWidget(
                0, self._empty_label(self._error, color=theme.DANGER))
        elif not items:
            self._list_layout.insertWidget(0, self._empty_label(*self._empty_text()))
        else:
            for index, item in enumerate(items):
                shown = redact_for_display(item.text or "", self._cfg, item.session or "")
                row = TodoRow(item, index, self._list_host, display_text=shown)
                row.toggled.connect(self._toggle)
                row.edited.connect(self._edit)
                row.detail_requested.connect(self._show_detail)
                row.menu_requested.connect(self._show_menu)
                self._list_layout.insertWidget(self._list_layout.count() - 1, row)
                self._rows.append(row)

        self._expand_btn.setVisible(hidden > 0)
        if hidden > 0:
            self._expand_btn.setText(f"展开 {hidden} 条")

    def _empty_text(self) -> tuple:
        if self._query:
            return (f"没有匹配「{self._query}」的条目", "换个关键词，或按 Esc 清除搜索")
        mapping = {
            "star": ("还没有重点条目", "在控制台把某人 / 某个群设为「特别关心」"),
            "maybe": ("没有待确认的条目", "规则拿不准的条目会先放这里，点一下确认"),
            "today": ("今天没有到期的待办", "今天没有截止项，轻松一点"),
            "done": ("还没有完成的记录", "完成一条就会出现在这里"),
            "todo": ("还没有待办", "同步一次后最多 5 秒自动出现，或在上面直接添加"),
        }
        return mapping.get(self._tab, ("没有待办", ""))

    def _empty_label(self, text: str, hint: str = "",
                     color: QColor | None = None) -> QLabel:
        label = QLabel(f"<div style='text-align:center'>"
                       f"<span style='font-size:13px'>{text}</span><br><br>"
                       f"<span style='font-size:11px; color:{theme.SUB.name()}'>{hint}</span>"
                       f"</div>")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        label.setContentsMargins(24, 40, 24, 40)
        label.setStyleSheet(
            f"color: {(color or theme.TEXT).name()}; background: transparent;"
        )
        return label

    def _update_footer(self, open_count: int) -> None:
        done_count = sum(1 for i in self._all if i.status == "done")
        extra = ""
        try:
            store = Store(self._db_path)
            try:
                ws = store.weekly_stats()
            finally:
                store.close()
            extra = f" · 本周清 {ws['done']} · 新增 {ws['new']}"
        except Exception:  # noqa: BLE001 - 统计失败不影响列表
            extra = ""
        self._stats.setText(f"待办 {open_count} · 已完成 {done_count}{extra}")

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------

    def _on_tab_changed(self, key: str) -> None:
        self._tab = key
        self._top_n_full = False
        self._render()

    def _on_search(self, text: str) -> None:
        self._query = (text or "").strip()
        self._render()

    def _on_add(self) -> None:
        text = self._add_edit.text().strip()
        if not text:
            return
        self._write(lambda s: s.add_todo(text))
        self._add_edit.clear()
        Toast.show_toast(self, "已添加待办")

    def _toggle(self, item: TodoItem) -> None:
        new_status = "pending" if item.status == "done" else "done"
        self._write(lambda s: s.set_status(item.todo_id, new_status))
        if new_status == "done":
            Toast.show_toast(self, "已完成", "撤销", lambda: self._restore(item))
        else:
            Toast.show_toast(self, "已恢复为待办")

    def _restore(self, item: TodoItem) -> None:
        self._write(lambda s: s.set_status(item.todo_id, "pending"))

    def _mark_noise(self, item: TodoItem) -> None:
        self._write(lambda s: s.set_status(item.todo_id, "noise"))
        Toast.show_toast(self, "已标为非待办", "撤销", lambda: self._restore(item))

    def _edit(self, item: TodoItem) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self, "修改内容", "待办内容：", item.text or "")
        if not ok:
            return
        text = text.strip()
        if not text or text == item.text:
            return
        self._write(lambda s: s.update_text(item.todo_id, text))
        Toast.show_toast(self, "已保存")

    def _clear_done(self) -> None:
        def action(store: Store) -> int:
            return store.archive_completed()

        moved = self._write(action)
        if moved:
            Toast.show_toast(self, f"已归档 {moved} 条完成项")
        else:
            Toast.show_toast(self, "今天还没有完成的条目")

    def _toggle_top_n(self) -> None:
        self._top_n_full = not self._top_n_full
        self._render()

    def _show_detail(self, item: TodoItem) -> None:
        # 详情里的原文与附近消息同样走展示脱敏，避免「列表打了码、弹窗漏原文」
        shown = redact_for_display(item.text or "", self._cfg, item.session or "")
        lines = [
            f"内容：{shown}",
            f"来源：{item.session or '—'} / {item.sender or '—'}",
            f"时间：{_fmt_ts(item.message_ts)}",
            f"状态：{item.status}",
            f"判定：{item.reason or '—'}（置信度 {item.confidence:.2f}）",
        ]
        try:
            store = Store(self._db_path)
            try:
                nearby = store.nearby_messages(item.message_ts, 4)
            finally:
                store.close()
            if nearby:
                lines.append("")
                lines.append("附近原文：")
                for msg in nearby:
                    content = redact_for_display(msg["content"] or "", self._cfg,
                                                 msg.get("session") or item.session or "")
                    lines.append(f"[{_fmt_ts(msg['ts'])}] {msg['sender_name']}：{content}")
        except Exception:  # noqa: BLE001
            pass
        box = QMessageBox(self)
        box.setWindowTitle("待办详情")
        box.setText(lines[0])
        box.setDetailedText("\n".join(lines[1:]))
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    def _show_menu(self, item: TodoItem, pos: QPoint) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: {theme.CARD_BG.name()}; color: {theme.TEXT.name()};"
            f" border: 1px solid {theme.BORDER.name()}; border-radius: 8px; padding: 4px; }}"
            f"QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 5px; font-size: 12px; }}"
            f"QMenu::item:selected {{ background: {theme.CARD_PRESSED.name()}; }}"
        )
        if item.status == "done":
            menu.addAction(self._action(menu, "恢复为待办", lambda: self._restore(item)))
        else:
            menu.addAction(self._action(menu, "标记完成", lambda: self._toggle(item)))
        menu.addAction(self._action(menu, "标为非待办", lambda: self._mark_noise(item)))
        menu.addSeparator()
        menu.addAction(self._action(menu, "修改内容", lambda: self._edit(item)))
        menu.addAction(self._action(menu, "查看原文", lambda: self._show_detail(item)))
        menu.addAction(self._action(menu, "判定理由", lambda: self._show_reason(item)))

        mute = menu.addMenu("此类不再提醒")
        mute.addAction(self._action(mute, "这条消息（同类）",
                                    lambda: self._mute(item, "text")))
        if item.sender:
            mute.addAction(self._action(mute, f"「{item.sender}」这个人",
                                        lambda: self._mute(item, "sender")))
        if item.session:
            mute.addAction(self._action(mute, f"「{item.session}」这个来源",
                                        lambda: self._mute(item, "session")))

        if _extract_filenames([item.text or ""]):
            menu.addAction(self._action(menu, "定位下载文件",
                                        lambda: self._locate_file(item)))

        menu.addSeparator()
        menu.addAction(self._action(menu, "移入回收站", lambda: self._trash(item)))
        menu.exec(pos)

    @staticmethod
    def _action(menu: QMenu, text: str, slot) -> QAction:
        action = QAction(text, menu)
        action.triggered.connect(lambda _checked=False: slot())
        return action

    def _trash(self, item: TodoItem) -> None:
        # trash_item 直接返回回收站记录 id，撤销时不必再猜「最后插入的是哪条」
        trash_id = self._write(lambda store: store.trash_item(item.todo_id))
        if trash_id:
            Toast.show_toast(self, "已移入回收站", "撤销",
                             lambda: self._write(lambda s: s.restore(trash_id)), 5000)

    def _mute(self, item: TodoItem, scope: str) -> None:
        """按 同类 / 此人 / 此来源 建静音规则，可在 Toast 上立刻撤销。"""
        session = item.session or ""
        if scope == "session":
            sender, text = "", ""
        elif scope == "sender":
            sender, text = item.sender or "", ""
        else:
            sender, text = "", item.text

        def action(store: Store):
            return store.mute(session, sender, text)

        result = self._write(action)
        if not result or not result.get("rule_id"):
            return
        rule_id = result["rule_id"]
        moved = result.get("muted_todos") or 0
        label = {"text": "同类消息", "sender": "这个人的消息",
                 "session": "这个来源的消息"}[scope]
        extra = f"（顺带收起 {moved} 条）" if moved else ""
        if result.get("created"):
            Toast.show_toast(self, f"以后不再提醒{label}{extra}", "撤销",
                             lambda: self._write(lambda s: s.unmute(rule_id)), 6000)
        else:
            # 规则本来就有（本次是命中旧规则），给撤销会误删你以前设的静音
            Toast.show_toast(self, f"以后不再提醒{label}{extra}（沿用原有规则）")

    def _show_reason(self, item: TodoItem) -> None:
        """展示判定依据：命中规则、加权分、阈值、校准影响。"""
        try:
            store = Store(self._db_path)
            try:
                report = store.explain_todo(item.todo_id)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001
            Toast.show_toast(self, f"读取判定失败：{exc}")
            return
        if not report:
            Toast.show_toast(self, "这条待办已经不在库里了")
            return

        lines = []
        for tag in report.get("tags") or []:
            mark = "✓" if tag.get("applied") else "·"
            lines.append(f"{mark} {tag.get('tag')}  +{tag.get('base_score')}"
                         f"  {tag.get('description') or ''}")
        lines.append("")
        lines.append(f"规则总分：{report.get('subtotal')}")
        delta = report.get("calib_delta") or 0
        if delta:
            lines.append(f"个人校准：{delta:+.2f}")
        lines.append(f"最终得分：{report.get('final_score')} / "
                     f"阈值 {report.get('threshold')}")
        lines.append(f"结论：{report['todo'].get('status')} · "
                     f"{report['todo'].get('reason') or '—'}")

        box = QMessageBox(self)
        box.setWindowTitle("判定理由")
        box.setText(f"为什么这条算待办？\n\n{(item.text or '')[:80]}")
        box.setDetailedText("\n".join(lines))
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    def _locate_file(self, item: TodoItem) -> None:
        """在资源管理器里选中与这条待办相关的下载文件。

        扫下载目录是几万次 stat，放主线程会把界面冻住几秒，所以丢给后台线程；
        同一时刻只允许一个扫描（连点右键不会堆线程）。
        """
        if self._locate_worker is not None:
            Toast.show_toast(self, "上一个文件还在找，稍等一下")
            return
        texts = [item.text or ""]
        try:
            store = Store(self._db_path)
            try:
                for msg in store.nearby_messages(item.message_ts, 6):
                    texts.append(msg["content"] or "")
            finally:
                store.close()
        except Exception:  # noqa: BLE001 - 找不到原文不影响按待办文本找
            pass
        names = _extract_filenames(texts)
        if not names:
            Toast.show_toast(self, "这条待办里没有可定位的文件名")
            return

        worker = _LocateWorker(names)
        worker.found.connect(self._on_located)
        worker.finished.connect(worker.deleteLater)
        self._locate_worker = worker
        Toast.show_toast(self, "正在下载目录里查找…")
        worker.start()

    def _on_located(self, path: str) -> None:
        self._locate_worker = None
        if not path:
            Toast.show_toast(self, "下载目录里没找到相关文件")
            return
        try:
            subprocess.Popen(["explorer", "/select,", path])
        except Exception as exc:  # noqa: BLE001
            Toast.show_toast(self, f"打开失败：{exc}")

    def _write(self, func) -> object:
        """统一的写库包装：写成功后强制刷新（不看 mtime，写的就是自己）。"""
        if not self._db_path:
            Toast.show_toast(self, "尚未配置数据库路径")
            return None
        try:
            store = Store(self._db_path)
            try:
                result = func(store)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("看板写库失败：%s", exc)
            Toast.show_toast(self, f"操作失败：{exc}")
            return None
        self.reload(force=True)
        self.data_changed.emit()
        return result

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self._search_edit.text():
            self._search_edit.clear()
            return
        super().keyPressEvent(event)
