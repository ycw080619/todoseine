"""看板主体（PySide6 DeskBox 风格），替换原 Tkinter 版 dashboard/app.py 的界面层。

信息架构与 Tk 版一一对应：
  标题栏（≡ / 待办 / 时钟 / 设置）
  → 顶部输入卡（内联添加 + 搜索）
  → 分段筛选（全部 / 重点 / 今日 / 待定 / 完成，带数量）
  → 待办卡片列表（勾选、强调色竖条、到期 chip、改 / ⋯ 菜单、双击看原文）
  → 页脚（统计 + 展开 + 清除已完成）

数据仍走 core.Store 与 core.dash_data，界面只读，勾选 / 编辑时才写回。
"""

from __future__ import annotations

import logging
import math
import os
import random
import subprocess
import time
from typing import List, Optional

from PySide6.QtCore import (QEasingCurve, QEvent, QMimeData, QPoint, QPointF,
                            QPropertyAnimation, QRectF, Qt, QThread, QTimer,
                            Signal, QVariantAnimation)
from PySide6.QtGui import (QAction, QColor, QDrag, QKeySequence, QPainter,
                           QPen, QShortcut)
from PySide6.QtWidgets import (QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QMenu, QMessageBox, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from .. import config as c2t_config
from ..core import Store
from ..core import forward
from ..core.deadline import label as due_label
from ..core.deadline import parse as due_parse
from ..core.privacy import redact_for_display
from ..core.dash_data import _is_today_due, filter_items, load_items
from ..core.dash_files import (_extract_filenames, _find_downloaded_file,
                               _fmt_ts)
from ..models import TodoItem
from .widgets import focus_ring, theme
from .widgets.card import CheckDot, RoundedCard
from .widgets.pill_tabs import PillTabs
from .widgets.text import ElidedLabel
from .widgets.toast import Toast

logger = logging.getLogger("todoseine.qt.dashboard")

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
        self._anim.setDuration(theme.motion_ms(180))
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
        delta = event.angleDelta().y()
        if not delta:
            return
        # 动画不在跑时以滚动条当前值为基准，避免列表刷新后第一滚跳变
        if self._anim.state() != QPropertyAnimation.State.Running:
            self._target = bar.value()
        # 滚轮一格(120)滚 ~72px（≈3 行文字）；不能用 bar.singleStep()，其默认值为 1px
        self._target -= int(delta * 0.6)
        self._target = max(bar.minimum(), min(bar.maximum(), self._target))
        if self._target == bar.value():
            return
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
    selection_toggled = Signal(object)  # 多选模式下点击行：通知看板切换选中

    HEIGHT = 62

    def __init__(self, item: TodoItem, index: int, parent: QWidget | None = None,
                 display_text: Optional[str] = None):
        super().__init__(parent, radius=14)
        self.item = item
        self._selected = False
        self._select_mode = False
        # 拖拽排序：记录按下位置，移动超过阈值才启动 QDrag
        self._drag_start_pos: Optional[QPoint] = None
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
        # 勾选点击由 TodoRow.mousePressEvent 按 geometry 判定，CheckDot 自身不收事件
        self.dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)

        title = ElidedLabel(("★ " if star else "") + self.display_text)
        title_font = theme.title_font(10)
        if item.status == "done":
            title_font.setStrikeOut(True)
        title.setFont(title_font)
        title.setTextColor(theme.SUB if item.status != "pending" else theme.TEXT)
        title.setStyleSheet("background: transparent;")
        # 透明传鼠标事件：hover 进出判定要落在卡片整体上，
        # 否则鼠标扫过文字时父卡片收 leaveEvent，hover 底色/边框高亮闪烁失效
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
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
        # 说明行里带日期/时间：等宽数字，避免「10:30 / 9:45」宽度差导致整行横跳
        meta_font = theme.number_font(8)
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
        meta.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
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

        # 勾选完成时的粒子迸发（只画在卡片内部，裁剪到圆角内）
        self._particles: List[dict] = []
        self._particle_t = 0.0
        self._particle_origin = QPoint(0, 0)
        self._particle_anim = QVariantAnimation(self)
        self._particle_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._particle_anim.valueChanged.connect(self._on_particle)
        self._particle_anim.finished.connect(self._clear_particles)

    # ---------- 勾选动效 ----------

    def play_done_effects(self) -> None:
        """标记完成：对勾笔画动画 + 从勾选点向外迸发一圈粒子。

        粒子画在本卡片内部（卡片不透明，避免半透明分层窗口的叠加风险），
        播完由看板重建列表收尾。
        """
        self.dot.setChecked(True, animated=True)
        ms = theme.motion_ms(460)
        if ms <= 0:
            return
        rng = random.Random()
        palette = [theme.ACCENT, theme.ACCENT_HINT,
                   theme.mix(theme.ACCENT, QColor("#ffffff"), 0.35),
                   self.bar_color]
        self._particles = []
        for _ in range(11):
            ang = rng.uniform(0.0, math.tau)
            speed = rng.uniform(46.0, 108.0)
            self._particles.append({
                "vx": math.cos(ang) * speed,
                "vy": math.sin(ang) * speed - 28.0,   # 略微上抛，配合重力回落
                "size": rng.uniform(1.8, 3.2),
                "color": rng.choice(palette),
            })
        self._particle_origin = self.dot.geometry().center()
        self._particle_anim.stop()
        self._particle_anim.setStartValue(0.0)
        self._particle_anim.setEndValue(1.0)
        self._particle_anim.setDuration(ms)
        self._particle_anim.start()

    def _on_particle(self, value) -> None:
        self._particle_t = float(value)
        self.update()

    def _clear_particles(self) -> None:
        self._particles = []
        self.update()

    # ---------- 绘制 ----------

    def paint_extra(self, painter: QPainter) -> None:
        # 选中态：整张卡片叠加一层主色半透明，比改边框更醒目但不刺眼
        if self._selected:
            sel = QColor(theme.ACCENT)
            sel.setAlphaF(0.16)
            path = self.rounded_path()
            painter.fillPath(path, sel)
        bar = QRectF(36.0, 15.0, 3.0, self.height() - 30.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.bar_color)
        painter.drawRoundedRect(bar, 1.5, 1.5)
        self.paint_focus_ring(painter)
        if self._particles:
            painter.save()
            painter.setClipPath(self.rounded_path())
            painter.setPen(Qt.PenStyle.NoPen)
            tsec = 0.46 * self._particle_t
            fade = max(0.0, 1.0 - self._particle_t)
            for pt in self._particles:
                x = self._particle_origin.x() + pt["vx"] * tsec
                y = (self._particle_origin.y() + pt["vy"] * tsec
                     + 0.5 * 260.0 * tsec * tsec)
                color = QColor(pt["color"])
                color.setAlphaF(min(1.0, fade * 1.4))
                painter.setBrush(color)
                s = pt["size"]
                painter.drawEllipse(QPointF(x, y), s, s)
            painter.restore()

    # ---------- 交互 ----------

    def _open_menu(self) -> None:
        pos = self.more_btn.mapToGlobal(QPoint(0, self.more_btn.height()))
        self.menu_requested.emit(self.item, pos)

    # ---------- 多选 ----------

    def set_select_mode(self, on: bool) -> None:
        self._select_mode = bool(on)
        if not on:
            self.set_selected(False)

    def set_selected(self, on: bool) -> None:
        if self._selected == bool(on):
            return
        self._selected = bool(on)
        self.update()

    def is_selected(self) -> bool:
        return self._selected

    # 勾选点命中区向外扩展的像素：视觉上还是 18px 圆点，但点击命中区放大到 ~34px，
    # 避免点偏落到卡片正文、想勾却没勾上的误触
    DOT_HIT_PAD = 8

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.position().toPoint()
            pad = self.DOT_HIT_PAD
            hit = self.dot.geometry().adjusted(-pad, -pad, pad, pad)
            self._press_in_dot = hit.contains(event.position().toPoint())
            if self._press_in_dot:
                self.dot.set_press(True)
            # 多选模式下：点卡片正文（非勾选点）切换选中
            if self._select_mode and not self._press_in_dot:
                self.set_selected(not self._selected)
                self.selection_toggled.emit(self.item)
                return
            self._animate_to(2.0)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (event.buttons() & Qt.MouseButton.LeftButton
                and self._drag_start_pos is not None
                and not self._select_mode):
            dist = (event.position().toPoint() - self._drag_start_pos).manhattanLength()
            # 超过 8px 且不是在勾选点上 → 启动拖拽排序
            if dist >= 8 and not self._press_in_dot:
                self._start_drag()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_start_pos = None
        self.dot.set_press(False)
        self._animate_to(1.0 if self.underMouse() else 0.0)
        if event.button() == Qt.MouseButton.LeftButton and self._press_in_dot:
            self._press_in_dot = False
            if self.rect().contains(event.position().toPoint()):
                self.toggled.emit(self.item)
        super().mouseReleaseEvent(event)

    def _start_drag(self) -> None:
        """启动拖拽：把 todo_id 放进 mime，看板收到 drop 后重排。"""
        if self.item.todo_id is None:
            return
        mime = QMimeData()
        mime.setText(str(self.item.todo_id))
        drag = QDrag(self)
        drag.setMimeData(mime)
        # 用卡片截图做拖拽预览，比默认的透明像素直观
        drag.setPixmap(self.grab())
        drag.exec(Qt.DropAction.MoveAction)

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


class _ProgressRing(QWidget):
    """页脚完成进度环：环形弧随「已完成 / 总数」联动填充。

    对应视频「勾选联动」里的进度环：勾选一条，弧长平滑增长一格。
    """

    SIZE = 20

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._pct = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    def set_pct(self, pct: float) -> None:
        pct = max(0.0, min(1.0, float(pct)))
        ms = theme.motion_ms(260)
        self._anim.stop()
        if ms <= 0 or not self.isVisible():
            self._pct = pct
            self.update()
            return
        self._anim.setDuration(ms)
        self._anim.setStartValue(self._pct)
        self._anim.setEndValue(pct)
        self._anim.start()

    def _on_anim(self, value) -> None:
        self._pct = float(value)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(2.5, 2.5, -2.5, -2.5)
        # 底轨
        p.setPen(QPen(QColor(255, 255, 255, 28), 2.0, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, 0, 360 * 16)
        # 前景弧：从顶部 90° 起，顺时针画 _pct 比例
        p.setPen(QPen(theme.ACCENT, 2.0, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        span = int(360 * 16 * self._pct)
        p.drawArc(rect, 90 * 16, -span)
        p.end()


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

        self._tab = "todo"
        self._query = ""
        self._all: List[TodoItem] = []
        self._rows: List[TodoRow] = []
        # 分批渲染：展开上百条时避免一次性创建数百个控件冻住主线程
        self._pending_items: List[TodoItem] = []
        self._pending_cfg = None
        self._mtime: Optional[float] = None
        self._day = time.strftime("%Y-%m-%d")
        self._error = ""
        self._locate_worker: Optional[_LocateWorker] = None
        # 页脚统计数字滚动：_stats_values 是当前显示值（浮点），向目标值插值
        self._stats_values: Optional[List[float]] = None
        self._stats_roll: Optional[tuple] = None   # (start, target)
        self._stats_anim = QVariantAnimation(self)
        self._stats_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._stats_anim.valueChanged.connect(self._on_stats_anim)
        # 多选模式
        self._select_mode = False
        self._selected_ids: set = set()

        self._build()
        self._scroll.anim_enabled = bool(dash.get("anim", True))
        self._clear_btn.setVisible(bool(dash.get("clear_done_button", True)))
        self._apply_enter_behavior()
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
        # 时钟是连续变化的数字：等宽数字 + 收紧字重，秒位不会左右跳
        self._clock.setFont(theme.number_font(9))
        self._clock.setStyleSheet(f"color: {theme.SUB.name()};")
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

        # 双环聚焦：边框宽度恒为 1px（聚焦只换颜色，不撑大控件），
        # 外环柔光由 focus_ring 的 drop shadow 负责
        _line_style = (
            "QLineEdit { background: transparent;"
            " border: 1px solid transparent; border-radius: 6px;"
            f" color: {theme.TEXT.name()}; font-size: 12px;"
            f" selection-background-color: {theme.css(theme.ACCENT)}; }}"
            "QLineEdit:focus {"
            f" border-color: {theme.css(theme.ACCENT)}; }}"
        )
        self._add_edit.setStyleSheet(_line_style)
        self._search_edit.setStyleSheet(
            _line_style
            + "QLineEdit { border-left: 1px solid rgba(255,255,255,32);"
            # 用 margin 而不是 padding：分隔线离输入卡边缘留出空隙，
            # 否则 1px 竖线紧贴圆角，看起来像一个「[」括号
            " margin-left: 8px; padding-left: 8px; }"
        )
        self._focus_glows = [focus_ring.attach(self._add_edit),
                             focus_ring.attach(self._search_edit)]
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
        self._list_host.setAcceptDrops(True)
        self._list_host.installEventFilter(self)
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 4, 0)
        self._list_layout.setSpacing(8)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(self._list_host)
        root.addWidget(self._scroll, 1)

        # ---- 页脚 ----
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self._ring = _ProgressRing(self)
        footer.addWidget(self._ring, 0, Qt.AlignmentFlag.AlignVCenter)
        self._stats = QLabel("")
        self._stats.setStyleSheet(
            f"color: {theme.SUB.name()}; font-size: 11px;"
        )
        footer.addWidget(self._stats)
        footer.addStretch(1)

        # 多选按钮：进入/退出批量操作模式
        self._select_btn = _pill_button("多选", accent=False)
        self._select_btn.clicked.connect(self._toggle_select_mode)
        footer.addWidget(self._select_btn)

        self._clear_btn = _pill_button("清除已完成")
        self._clear_btn.clicked.connect(self._clear_done)
        footer.addWidget(self._clear_btn)

        # 批量操作栏：多选模式下替换页脚按钮
        self._batch_bar = QHBoxLayout()
        self._batch_bar.setSpacing(6)
        self._batch_count = QLabel("已选 0 条")
        self._batch_count.setStyleSheet(
            f"color: {theme.TEXT.name()}; font-size: 11px;")
        self._batch_bar.addWidget(self._batch_count)
        self._batch_bar.addStretch(1)
        self._batch_all_btn = _pill_button("全选", accent=False)
        self._batch_all_btn.clicked.connect(self._select_all)
        self._batch_bar.addWidget(self._batch_all_btn)
        self._batch_done_btn = _pill_button("完成", accent=False)
        self._batch_done_btn.clicked.connect(lambda: self._batch_action("done"))
        self._batch_bar.addWidget(self._batch_done_btn)
        self._batch_star_btn = _pill_button("标重点", accent=False)
        self._batch_star_btn.clicked.connect(lambda: self._batch_action("star"))
        self._batch_bar.addWidget(self._batch_star_btn)
        self._batch_trash_btn = _pill_button("删除", accent=True)
        self._batch_trash_btn.clicked.connect(lambda: self._batch_action("trash"))
        self._batch_bar.addWidget(self._batch_trash_btn)
        self._batch_cancel_btn = _pill_button("取消", accent=False)
        self._batch_cancel_btn.clicked.connect(self._exit_select_mode)
        self._batch_bar.addWidget(self._batch_cancel_btn)
        # 批量栏默认隐藏，_toggle_select_mode 时和普通按钮互换可见性
        for i in range(self._batch_bar.count()):
            item = self._batch_bar.itemAt(i)
            w = item.widget() if item else None
            if w is not None:
                w.hide()
        root.addLayout(self._batch_bar)
        root.addLayout(footer)
        self._footer_layout = footer

        QShortcut(QKeySequence.StandardKey.Find, self,
                  activated=self._search_edit.setFocus)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """点输入框时请求主窗口临时取消 NOACTIVATE，否则打不了字。"""
        if event.type() == QEvent.Type.MouseButtonPress and obj in (
                self._add_edit, self._search_edit):
            self.activation_requested.emit()
            edit = obj
            QTimer.singleShot(0, edit.setFocus)
        # 回车行为：ctrl_enter 模式下，裸回车不提交（留给 Ctrl+回车）
        if (obj is self._add_edit
                and event.type() == QEvent.Type.KeyPress
                and getattr(self, "_enter_needs_ctrl", False)
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not (event.modifiers() & Qt.KeyboardModifier.ControlModifier)):
            return True
        # 拖拽排序：接收 TodoRow 拖过来的 todo_id，按落点重排
        if obj is self._list_host:
            et = event.type()
            if et == QEvent.Type.DragEnter:
                if event.mimeData().hasText():
                    event.acceptProposedAction()
                    return True
            elif et == QEvent.Type.DragMove:
                event.acceptProposedAction()
                return True
            elif et == QEvent.Type.Drop:
                self._handle_drop(event)
                event.acceptProposedAction()
                return True
        return super().eventFilter(obj, event)

    def _handle_drop(self, event) -> None:
        """拖拽落点：把被拖的条目插到落点所在行之前。"""
        try:
            dragged_id = int(event.mimeData().text())
        except (ValueError, TypeError):
            return
        # 找到落点对应的行索引
        pos = event.position() if hasattr(event, "position") else event.pos()
        drop_pos = self._list_host.mapFromGlobal(pos.toPoint())
        target_idx = None
        for i, row in enumerate(self._rows):
            if row.geometry().contains(drop_pos):
                target_idx = i
                break
        if target_idx is None:
            return
        # 被拖的行当前索引
        src_idx = None
        for i, row in enumerate(self._rows):
            if row.item.todo_id == dragged_id:
                src_idx = i
                break
        if src_idx is None or src_idx == target_idx:
            return
        # 重排 self._rows 的顺序
        # pop 之后：若源在目标之前，目标已左移一位，插入点要减 1 才能落在目标之前
        row = self._rows.pop(src_idx)
        insert_at = target_idx if src_idx > target_idx else target_idx - 1
        self._rows.insert(insert_at, row)
        # 同步布局：移除再插回
        self._list_layout.removeWidget(row)
        self._list_layout.insertWidget(insert_at, row)
        # 写库：按当前顺序更新所有可见条目的 display_order
        ordered = [r.item.todo_id for r in self._rows if r.item.todo_id is not None]
        self._write(lambda s: s.reorder(ordered))

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

        # 分批渲染定时器：展开大量条目时每 16ms 追加一批，保持 UI 响应
        self._batch_timer = QTimer(self)
        self._batch_timer.setInterval(16)
        self._batch_timer.timeout.connect(self._render_next_batch)

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
        self._clock_timer.blockSignals(not bool(dash.get("clock", True)))
        self._clock.setVisible(bool(dash.get("clock", True)))
        self._scroll.anim_enabled = bool(dash.get("anim", True))
        self._clear_btn.setVisible(bool(dash.get("clear_done_button", True)))
        self._apply_enter_behavior()
        self.reload(force=True)

    def _apply_enter_behavior(self) -> None:
        """回车行为：enter=直接添加；ctrl_enter=Ctrl+回车才添加。

        QLineEdit.returnPressed 拿不到修饰键，所以在 eventFilter 里
        拦截 KeyPress 自己判（见 eventFilter 的 Enter 分支）。
        """
        dash = self._cfg.get("dash") or {}
        self._enter_needs_ctrl = (str(dash.get("todo_enter") or "enter")
                                  .lower() == "ctrl_enter")

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

        open_count = sum(1 for i in self._all if i.status != "done")
        self._tabs.set_counts({
            "todo": tab_len,
            "maybe": sum(1 for i in self._all if i.status == "unconfirmed"),
            "star": sum(1 for i in self._all if "★重点" in (i.reason or "")),
            "today": sum(1 for i in self._all if _is_today_due(i)),
            "done": None,
        })

        self._fill_list(tab_items)
        self._update_footer(open_count)

    def _clear_rows(self) -> None:
        for row in self._rows:
            self._list_layout.removeWidget(row)
            row.deleteLater()
        self._rows = []

    # 分批渲染阈值：超过这个数量就分批创建控件，避免主线程长时间阻塞
    BATCH_THRESHOLD = 60
    BATCH_SIZE = 40

    def _make_row(self, item: TodoItem, index: int) -> TodoRow:
        """创建单条待办行并连接信号。"""
        shown = redact_for_display(item.text or "", self._cfg, item.session or "")
        row = TodoRow(item, index, self._list_host, display_text=shown)
        row.toggled.connect(self._toggle)
        row.edited.connect(self._edit)
        row.detail_requested.connect(self._show_detail)
        row.menu_requested.connect(self._show_menu)
        row.selection_toggled.connect(self._on_selection_toggled)
        row.set_select_mode(self._select_mode)
        if item.todo_id in self._selected_ids:
            row.set_selected(True)
        return row

    def _fill_list(self, items: List[TodoItem]) -> None:
        # 取消上一次未完成的分批渲染
        self._batch_timer.stop()
        self._pending_items = []

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
            # 条目多时只先渲染首批，剩余的交给 _render_next_batch 逐批追加
            if len(items) > self.BATCH_THRESHOLD:
                first = items[:self.BATCH_THRESHOLD]
                rest = items[self.BATCH_THRESHOLD:]
            else:
                first = items
                rest = []

            for index, item in enumerate(first):
                row = self._make_row(item, index)
                self._list_layout.insertWidget(self._list_layout.count() - 1, row)
                self._rows.append(row)

            if rest:
                self._pending_items = rest
                self._pending_cfg = self._cfg
                self._batch_timer.start()

    def _render_next_batch(self) -> None:
        """分批渲染的定时器回调：每帧追加 BATCH_SIZE 条，直到全部完成。"""
        if not self._pending_items:
            self._batch_timer.stop()
            return
        batch = self._pending_items[:self.BATCH_SIZE]
        self._pending_items = self._pending_items[self.BATCH_SIZE:]
        base = len(self._rows)
        for offset, item in enumerate(batch):
            row = self._make_row(item, base + offset)
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)
            self._rows.append(row)
        if not self._pending_items:
            self._batch_timer.stop()

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
        # 页脚统计可关（DeskBox 的 todoShowFooterStats），关掉后只留空白占位
        dash = self._cfg.get("dash") or {}
        if not bool(dash.get("footer_stats", True)):
            self._stats.setText("")
            self._ring.hide()
            self._stats_values = None
            return
        done_count = sum(1 for i in self._all if i.status == "done")
        total = open_count + done_count
        self._ring.show()
        self._ring.set_pct(done_count / total if total else 0.0)
        weekly: Optional[List[float]] = None
        try:
            store = Store(self._db_path)
            try:
                ws = store.weekly_stats()
            finally:
                store.close()
            weekly = [float(ws["done"]), float(ws["new"])]
        except Exception:  # noqa: BLE001 - 统计失败不影响列表
            weekly = None
        target = [float(open_count), float(done_count)] + (weekly or [])
        self._roll_stats(target)

    @staticmethod
    def _stats_text(values: List[float]) -> str:
        nums = [int(round(v)) for v in values]
        text = f"待办 {nums[0]} · 已完成 {nums[1]}"
        if len(nums) >= 4:
            text += f" · 本周清 {nums[2]} · 新增 {nums[3]}"
        return text

    def _roll_stats(self, target: List[float]) -> None:
        """统计数字滚动：勾选完成时「待办 5 → 4」平滑过渡，不跳变。"""
        prev = self._stats_values
        ms = theme.motion_ms(260)
        if (prev is None or len(prev) != len(target)
                or all(abs(a - b) < 0.5 for a, b in zip(prev, target))
                or ms <= 0 or not self.isVisible() or not self._anim_enabled()):
            self._stats_anim.stop()
            self._stats_values = list(target)
            self._stats.setText(self._stats_text(target))
            return
        self._stats_roll = (list(prev), list(target))
        self._stats_anim.stop()
        self._stats_anim.setStartValue(0.0)
        self._stats_anim.setEndValue(1.0)
        self._stats_anim.setDuration(ms)
        self._stats_anim.start()

    def _on_stats_anim(self, value) -> None:
        if not self._stats_roll:
            return
        start, target = self._stats_roll
        t = float(value)
        self._stats_values = [a + (b - a) * t for a, b in zip(start, target)]
        self._stats.setText(self._stats_text(self._stats_values))

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------

    def _on_tab_changed(self, key: str) -> None:
        self._tab = key
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

    def _row_for(self, item: TodoItem) -> Optional[TodoRow]:
        for row in self._rows:
            if row.item.todo_id == item.todo_id:
                return row
        return None

    def _anim_enabled(self) -> bool:
        return bool((self._cfg.get("dash") or {}).get("anim", True))

    def _toggle(self, item: TodoItem) -> None:
        new_status = "pending" if item.status == "done" else "done"
        # 先播行内动效（对勾笔画 + 粒子），播完再重建列表，
        # 否则 reload 会把正在播动画的行直接销毁。
        # 延迟只等对勾画完（~200ms），粒子被截断无所谓——行马上要被移除。
        defer = 0
        row = self._row_for(item)
        if row is not None and self._anim_enabled():
            if new_status == "done":
                defer = theme.motion_ms(200)
                if defer > 0:
                    row.play_done_effects()
            else:
                defer = theme.motion_ms(150)
                if defer > 0:
                    row.dot.setChecked(False, animated=True)
        self._write(lambda s: s.set_status(item.todo_id, new_status),
                    defer_reload_ms=defer)
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

    # ---------- 多选 / 批量操作 ----------

    def _toggle_select_mode(self) -> None:
        self._set_select_mode(not self._select_mode)

    def _exit_select_mode(self) -> None:
        self._set_select_mode(False)

    def _set_select_mode(self, on: bool) -> None:
        self._select_mode = bool(on)
        for row in self._rows:
            row.set_select_mode(self._select_mode)
        # 互换页脚按钮可见性
        normal_widgets = [self._select_btn, self._clear_btn]
        for w in normal_widgets:
            w.setVisible(not on)
        for i in range(self._batch_bar.count()):
            item = self._batch_bar.itemAt(i)
            w = item.widget() if item else None
            if w is not None:
                w.setVisible(on)
        if not on:
            self._selected_ids.clear()
        self._update_batch_bar()

    def _on_selection_toggled(self, item: TodoItem) -> None:
        if item.todo_id is None:
            return
        if item.todo_id in self._selected_ids:
            self._selected_ids.discard(item.todo_id)
        else:
            self._selected_ids.add(item.todo_id)
        self._update_batch_bar()

    def _select_all(self) -> None:
        self._selected_ids.clear()
        for row in self._rows:
            if row.item.todo_id is not None:
                self._selected_ids.add(row.item.todo_id)
                row.set_selected(True)
        self._update_batch_bar()

    def _update_batch_bar(self) -> None:
        self._batch_count.setText(f"已选 {len(self._selected_ids)} 条")

    def _batch_action(self, action: str) -> None:
        if not self._selected_ids:
            return
        ids = list(self._selected_ids)
        if action == "trash":
            if QMessageBox.question(
                    self, "批量删除",
                    f"确定删除选中的 {len(ids)} 条？删除后可在回收站还原。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return

        def op(store: Store) -> int:
            n = 0
            for tid in ids:
                if action == "done":
                    store.set_status(tid, "done")
                    n += 1
                elif action == "star":
                    store.set_star(tid, True)
                    n += 1
                elif action == "trash":
                    store.trash_item(tid)
                    n += 1
            return n

        count = self._write(op)
        if count:
            label = {"done": "完成", "star": "标为重点", "trash": "删除"}[action]
            Toast.show_toast(self, f"已{label} {count} 条")
        self._selected_ids.clear()
        self._set_select_mode(False)

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

        # 半自动转发是高风险可选功能：只有用户在设置里显式开启才出现
        if forward.enabled(self._cfg):
            menu.addSeparator()
            menu.addAction(self._action(menu, "转发…（预览后确认）",
                                        lambda: self._forward(item)))

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

    def _forward(self, item: TodoItem) -> None:
        """半自动转发：弹预览窗，倒计时结束后由用户手动确认。"""
        from .forward_dialog import run as run_forward

        # 转发出去的是脱敏后的展示文本（与看板一致的对外口径）
        text = redact_for_display(item.text or "", self._cfg, item.session or "")
        target = item.session or item.sender or ""
        if not run_forward(text, target, self._cfg, self):
            Toast.show_toast(self, "已取消转发")

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

    def _write(self, func, defer_reload_ms: int = 0) -> object:
        """统一的写库包装：写成功后强制刷新（不看 mtime，写的就是自己）。

        defer_reload_ms > 0 时把界面重建推迟到行内动画播完（勾选动效）：
        先同步 mtime，防止 3 秒周期刷新定时器提前重建列表、打断动画。
        """
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
        if defer_reload_ms > 0:
            self._mtime = self._db_mtime()
            QTimer.singleShot(defer_reload_ms, lambda: self.reload(force=True))
            QTimer.singleShot(defer_reload_ms, self.data_changed.emit)
        else:
            self.reload(force=True)
            self.data_changed.emit()
        return result

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self._search_edit.text():
            self._search_edit.clear()
            return
        super().keyPressEvent(event)
