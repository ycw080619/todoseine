"""看板之外的三个辅助页：回收站 / 统计 / 关于。

共同约定：
  - 只做只读展示，写操作全部是一次性确认 + 立刻刷新；
  - 数据来源仍走 core.Store，页面自己不缓存跨页状态；
  - 每页 refresh() 都在切页时被 main_window 调用，保证看到的是最新库。
"""

from __future__ import annotations

import os
import platform
import sys
from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from .. import __version__
from .. import config as c2t_config
from ..core import Store
from .widgets import theme
from .widgets.text import ElidedLabel


# ---------------------------------------------------------------------------
# 公共小件
# ---------------------------------------------------------------------------

def _btn(text: str, accent: bool = False) -> QPushButton:
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFixedHeight(26)
    if accent:
        b.setStyleSheet(
            f"QPushButton {{ background: {theme.ACCENT.name()}; border: none;"
            f" color: {theme.ACCENT_TEXT.name()}; border-radius: 13px;"
            " font-size: 11px; padding: 0 14px; }"
            f"QPushButton:hover {{ background: "
            f"{theme.mix(theme.ACCENT, theme.PANEL, 0.15).name()}; }}"
        )
    else:
        b.setStyleSheet(
            f"QPushButton {{ background: {theme.CARD_BG.name()};"
            f" color: {theme.SUB.name()}; border: 1px solid {theme.CARD_BORDER.name()};"
            " border-radius: 13px; font-size: 11px; padding: 0 14px; }"
            f"QPushButton:hover {{ background: {theme.CARD_HOVER.name()};"
            f" color: {theme.TEXT.name()}; }}"
        )
    return b


def _card_frame() -> tuple:
    """返回 (卡片容器, 竖向布局)，统一圆角/描边。"""
    frame = QFrame()
    frame.setObjectName("PageCard")
    frame.setStyleSheet(
        f"#PageCard {{ background: {theme.CARD_BG.name()};"
        f" border: 1px solid {theme.CARD_BORDER.name()}; border-radius: 14px; }}"
    )
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(14, 12, 14, 12)
    lay.setSpacing(6)
    return frame, lay


def _muted(text: str, size: int = 11) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet(f"color: {theme.SUB.name()}; font-size: {size}px;"
                      " background: transparent;")
    return lab


def _title(text: str) -> QLabel:
    lab = QLabel(text)
    f = QFont(theme.FONT_FAMILY)
    f.setPointSize(10)
    f.setBold(True)
    lab.setFont(f)
    lab.setStyleSheet(f"color: {theme.TEXT.name()}; background: transparent;")
    return lab


def _open_store(db_path: str) -> Optional[Store]:
    if not db_path:
        return None
    try:
        return Store(db_path)
    except Exception:  # noqa: BLE001 - 数据库损坏时页面显示空态即可
        return None


# ---------------------------------------------------------------------------
# 回收站
# ---------------------------------------------------------------------------

class TrashPage(QWidget):
    """误删兜底：还原 / 彻底删除 / 清空。"""

    changed = Signal()      # 回收站内容变化，通知看板刷新

    def __init__(self, db_path: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._db_path = db_path
        self._build()
        self.refresh()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 8, 16, 12)
        root.setSpacing(10)

        head = QHBoxLayout()
        self._count = _muted("回收站为空")
        head.addWidget(self._count)
        head.addStretch(1)
        clear_btn = _btn("清空回收站")
        clear_btn.clicked.connect(self._clear_all)
        head.addWidget(clear_btn)
        root.addLayout(head)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self._body = QWidget()
        self._body.setStyleSheet("background: transparent;")
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 0, 0, 0)
        self._body_lay.setSpacing(8)
        self._body_lay.addStretch(1)
        self._scroll.setWidget(self._body)
        root.addWidget(self._scroll, 1)

    def set_db_path(self, path: str) -> None:
        self._db_path = path
        self.refresh()

    def refresh(self) -> None:
        while self._body_lay.count() > 1:
            item = self._body_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        store = _open_store(self._db_path)
        rows: List[dict] = []
        try:
            if store is not None:
                rows = store.list_trash(100)
                total = store.count_trash()
            else:
                total = 0
        finally:
            if store is not None:
                store.close()

        self._count.setText(f"共 {total} 条，保留最近删除的 100 条" if total
                            else "回收站为空")
        if not rows:
            empty = _muted("误删的待办会先落在这里，可以随时还原")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._body_lay.insertWidget(0, empty)
            return

        for row in rows:
            self._body_lay.insertWidget(self._body_lay.count() - 1,
                                        self._row(row))

    def _row(self, row: dict) -> QWidget:
        frame, lay = _card_frame()
        top = QHBoxLayout()
        top.setSpacing(8)

        text = ElidedLabel(row.get("text") or "")
        f = QFont(theme.FONT_FAMILY)
        f.setPointSize(9)
        text.setFont(f)
        text.setTextColor(theme.TEXT)
        text.setStyleSheet("background: transparent;")
        top.addWidget(text, 1)

        restore_btn = _btn("还原", accent=True)
        restore_btn.clicked.connect(lambda: self._restore(int(row["id"])))
        top.addWidget(restore_btn)

        purge_btn = _btn("彻底删除")
        purge_btn.clicked.connect(lambda: self._purge(int(row["id"])))
        top.addWidget(purge_btn)
        lay.addLayout(top)

        meta = " · ".join(x for x in [
            row.get("session") or "",
            row.get("sender") or "",
            f"删除于 {row.get('deleted_at') or '—'}",
        ] if x)
        lay.addWidget(_muted(meta))
        return frame

    # ---------- 写操作 ----------

    def _restore(self, trash_id: int) -> None:
        store = _open_store(self._db_path)
        if store is None:
            return
        try:
            ok = store.restore(trash_id)
        finally:
            store.close()
        if not ok:
            QMessageBox.information(self, "还原失败",
                                    "同一条待办已经在列表里了，回收站记录保留。")
        self.refresh()
        self.changed.emit()

    def _purge(self, trash_id: int) -> None:
        store = _open_store(self._db_path)
        if store is None:
            return
        try:
            store.conn.execute("DELETE FROM trash WHERE id=?", (trash_id,))
            store.conn.commit()
        finally:
            store.close()
        self.refresh()

    def _clear_all(self) -> None:
        store = _open_store(self._db_path)
        if store is None:
            return
        try:
            total = store.count_trash()
        finally:
            store.close()
        if not total:
            return
        if QMessageBox.question(
                self, "清空回收站",
                f"确定永久删除 {total} 条记录？此操作不可撤销。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        store = _open_store(self._db_path)
        if store is None:
            return
        try:
            store.clear_trash()
        finally:
            store.close()
        self.refresh()


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

class StatsPage(QWidget):
    """总览 + 近 7 天 + 校准情况，纯查询。"""

    def __init__(self, db_path: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._db_path = db_path
        self._build()
        self.refresh()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 8, 16, 12)
        root.setSpacing(10)

        self._grid = QVBoxLayout()
        self._grid.setSpacing(10)
        root.addLayout(self._grid)
        root.addStretch(1)

        head = QHBoxLayout()
        self._hint = _muted("")
        head.addWidget(self._hint, 1)
        refresh_btn = _btn("刷新")
        refresh_btn.clicked.connect(self.refresh)
        head.addWidget(refresh_btn)
        root.addLayout(head)

    def set_db_path(self, path: str) -> None:
        self._db_path = path
        self.refresh()

    def refresh(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is None and item.layout() is not None:
                self._clear_layout(item.layout())
                continue
            if w is not None:
                w.deleteLater()

        store = _open_store(self._db_path)
        if store is None:
            self._hint.setText("尚未配置数据库路径，去「设置」里填一个")
            self._grid.addWidget(self._card("暂无数据", []))
            return
        try:
            stats = store.stats()
            week = store.weekly_stats()
            calib = store.calibration_stats()
            mutes = len(store.list_mutes())
            trash = store.count_trash()
        finally:
            store.close()

        self._hint.setText("统计范围：整个数据库")
        self._grid.addWidget(self._card("总览", [
            ("消息总数", stats.get("messages", 0)),
            ("待办总数", stats.get("todos", 0)),
            ("待完成", stats.get("pending", 0)),
            ("待确认", stats.get("unconfirmed", 0)),
            ("已完成", stats.get("done", 0)),
            ("已静音", stats.get("muted", 0)),
            ("回收站", trash),
        ]))
        top = week.get("top_chat")
        self._grid.addWidget(self._card("近 7 天", [
            ("新增待办", week.get("new", 0)),
            ("完成待办", week.get("done", 0)),
            ("最活跃来源", f"{top[0]} · {top[1]} 条" if top else "—"),
            ("静音规则", mutes),
        ]))
        actions = [(f"标注 · {k}", v) for k, v in calib.items()
                   if k not in ("total", "unique_keys")]
        self._grid.addWidget(self._card("判定校准", [
            ("标注样本", calib.get("total", 0)),
            ("涉及联系人", calib.get("unique_keys", 0)),
        ] + actions))

    def _card(self, title: str, pairs) -> QWidget:
        frame, lay = _card_frame()
        lay.addWidget(_title(title))
        if not pairs:
            lay.addWidget(_muted("—"))
            return frame
        for name, value in pairs:
            row = QHBoxLayout()
            row.addWidget(_muted(str(name), 12))
            row.addStretch(1)
            val = QLabel(str(value))
            val.setStyleSheet(f"color: {theme.TEXT.name()}; font-size: 13px;"
                              " font-weight: 600; background: transparent;")
            row.addWidget(val)
            lay.addLayout(row)
        return frame

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()


# ---------------------------------------------------------------------------
# 关于
# ---------------------------------------------------------------------------

class AboutPage(QWidget):
    """版本、路径、诊断信息，以及两个常用入口。"""

    def __init__(self, db_path: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._cfg = c2t_config.load()
        self._db_path = db_path or str(self._cfg.get("db_path") or "")
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 8, 16, 12)
        root.setSpacing(12)

        frame, lay = _card_frame()
        lay.addWidget(_title(f"Chat2Todo {__version__}"))
        lay.addWidget(_muted("QQ 群聊消息 → 待办清单，本地运行，不联网也能用"))

        info = [
            ("Python", platform.python_version()),
            ("PySide6", self._pyside_version()),
            ("系统", f"{platform.system()} {platform.release()}"),
            ("数据库", self._cfg.get("db_path") or "未配置"),
            ("配置文件", self._cfg.get("config_file") or "默认值（尚未写入）"),
            ("日志目录", os.path.expanduser("~/.chat2todo")),
        ]
        for name, value in info:
            row = QHBoxLayout()
            row.addWidget(_muted(f"{name}：", 12))
            val = QLabel(str(value))
            val.setStyleSheet(f"color: {theme.TEXT.name()}; font-size: 12px;"
                              " background: transparent;")
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(val, 1)
            lay.addLayout(row)
        root.addWidget(frame)

        tip, tlay = _card_frame()
        tlay.addWidget(_title("操作提示"))
        for line in [
            "· 托盘双击图标：显示 / 收起看板",
            "· Ctrl+Alt+D：随时唤出看板",
            "· Ctrl+Shift+S：框选截图建待办",
            "· 看板右上「设置」：监听间隔、AI、外观、热键",
        ]:
            tlay.addWidget(_muted(line, 12))
        root.addWidget(tip)

        btns = QHBoxLayout()
        open_btn = _btn("打开数据目录", accent=True)
        open_btn.clicked.connect(self._open_data_dir)
        btns.addWidget(open_btn)

        copy_btn = _btn("复制诊断信息")
        copy_btn.clicked.connect(self._copy_diag)
        btns.addWidget(copy_btn)
        btns.addStretch(1)
        root.addLayout(btns)
        root.addStretch(1)

    @staticmethod
    def _pyside_version() -> str:
        try:
            import PySide6

            return PySide6.__version__
        except Exception:  # noqa: BLE001
            return "未安装"

    def _open_data_dir(self) -> None:
        path = os.path.dirname(os.path.abspath(self._db_path or ""))
        if path and os.path.isdir(path):
            os.startfile(path)  # type: ignore[attr-defined]

    def _copy_diag(self) -> None:
        text = "\n".join([
            f"Chat2Todo {__version__}",
            f"Python {platform.python_version()} / PySide6 {self._pyside_version()}",
            f"{platform.platform()}",
            f"frozen={getattr(sys, 'frozen', False)}",
            f"db={self._cfg.get('db_path')}",
            f"config={self._cfg.get('config_file')}",
        ])
        QApplication.clipboard().setText(text)
        QMessageBox.information(self, "已复制", "诊断信息已复制到剪贴板。")
