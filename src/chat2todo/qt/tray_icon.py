"""托盘图标 + 右键菜单。

图标用 QPainter 现画（不依赖外部 .ico，打包后也不用额外带资源）：
深色圆角方块 + 强调色对勾，16/32/64 三个尺寸都清晰。

菜单项与 Tk 版控制台一一对应：
显示主窗口 / 立即同步 / 暂停监听 / 开机自启 / 打开数据目录 / 退出
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Optional

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from . import tray


def _draw_icon(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    plate = QRectF(size * 0.06, size * 0.06, size * 0.88, size * 0.88)
    path = QPainterPath()
    path.addRoundedRect(plate, size * 0.24, size * 0.24)
    p.fillPath(path, QColor("#4cc2ff"))

    pen = QPen(QColor("#0b1a22"))
    pen.setWidthF(max(1.6, size * 0.11))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    tick = QPainterPath(QPointF(size * 0.28, size * 0.52))
    tick.lineTo(size * 0.44, size * 0.68)
    tick.lineTo(size * 0.74, size * 0.34)
    p.drawPath(tick)
    p.end()
    return pm


def build_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128):
        icon.addPixmap(_draw_icon(size))
    return icon


class TrayController(QObject):
    """把托盘的所有行为收在一处，main.py 只负责接线。"""

    quit_requested = Signal()

    def __init__(self, window: QWidget, monitor, db_path_provider: Callable[[], str],
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self._window = window
        self._monitor = monitor
        self._db_path = db_path_provider

        self.icon = QSystemTrayIcon(build_icon(), self)
        self.icon.setToolTip("Chat2Todo")
        self.icon.setContextMenu(self._build_menu())
        self.icon.activated.connect(self._on_activated)
        self.icon.show()

        monitor.status_changed.connect(self._on_status)
        monitor.todos_added.connect(self._on_new_todos)
        self._on_status(monitor.status())

    # ---------- 菜单 ----------

    def _build_menu(self) -> QMenu:
        menu = QMenu()
        self._act_show = QAction("显示主窗口", menu)
        self._act_show.triggered.connect(self.show_window)
        menu.addAction(self._act_show)

        self._act_sync = QAction("立即同步", menu)
        self._act_sync.triggered.connect(self._monitor.sync_now)
        menu.addAction(self._act_sync)

        self._act_pause = QAction("暂停监听", menu)
        self._act_pause.setCheckable(True)
        self._act_pause.setChecked(self._monitor.is_paused())
        self._act_pause.toggled.connect(self._on_pause_toggled)
        menu.addAction(self._act_pause)

        menu.addSeparator()

        self._act_boot = QAction("开机自启", menu)
        self._act_boot.setCheckable(True)
        self._act_boot.setChecked(tray.is_autostart_enabled())
        self._act_boot.toggled.connect(self._on_boot_toggled)
        menu.addAction(self._act_boot)

        act_dir = QAction("打开数据目录", menu)
        act_dir.triggered.connect(self._open_data_dir)
        menu.addAction(act_dir)

        menu.addSeparator()
        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self.quit_requested.emit)
        menu.addAction(act_quit)
        return menu

    # ---------- 行为 ----------

    def show_window(self) -> None:
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.DoubleClick,
                      QSystemTrayIcon.ActivationReason.Trigger):
            if self._window.isVisible() and self._window.isActiveWindow():
                self._window.hide()
            else:
                self.show_window()

    def _on_pause_toggled(self, checked: bool) -> None:
        if checked:
            self._monitor.pause()
            self.notify("已暂停监听", "点托盘菜单「暂停监听」可恢复")
        else:
            self._monitor.resume()

    def _on_boot_toggled(self, checked: bool) -> None:
        if not tray.set_autostart(checked):
            self._act_boot.blockSignals(True)
            self._act_boot.setChecked(not checked)
            self._act_boot.blockSignals(False)
            self.notify("开机自启设置失败", "可能需要管理员权限")

    def _open_data_dir(self) -> None:
        path = os.path.dirname(os.path.abspath(self._db_path() or ""))
        if not path or not os.path.isdir(path):
            self.notify("数据目录还不存在", "先同步一次或检查数据库路径")
            return
        try:
            subprocess.Popen(["explorer", path])
        except Exception:  # noqa: BLE001 - explorer 正常不会失败
            os.startfile(path)  # type: ignore[attr-defined]

    # ---------- 通知 ----------

    def notify(self, title: str, text: str, msec: int = 4000) -> None:
        if not self.icon.isVisible():
            return
        self.icon.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, msec)

    def _on_status(self, text: str) -> None:
        self.icon.setToolTip(f"Chat2Todo · {text}")
        # 暂停状态可能从设置页 / 热键切换，菜单勾选要跟着走
        paused = self._monitor.is_paused()
        if self._act_pause.isChecked() != paused:
            self._act_pause.blockSignals(True)
            self._act_pause.setChecked(paused)
            self._act_pause.blockSignals(False)

    def _on_new_todos(self, items) -> None:
        if not items:
            return
        # 窗口开着时由看板自己弹 Toast，这里只在藏起来时打扰用户
        if self._window.isVisible():
            return
        first = items[0]
        if len(items) == 1:
            self.notify("新待办", (first.text or "")[:80])
        else:
            self.notify(f"新增 {len(items)} 条待办",
                        (first.text or "")[:60] + f" 等 {len(items)} 条")
