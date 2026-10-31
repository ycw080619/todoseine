"""独立子窗口 —— 设置 / 统计 / 关于。

这些窗口是普通 QMainWindow，不走 WA_TranslucentBackground 也不用 DWM 亚克力，
彻底绕开分层窗口合成路径下「首个不透明子控件水平镜像」的 Qt bug。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QMainWindow, QTabWidget, QWidget


class SettingsWindow(QMainWindow):
    """设置面板独立窗口。

    把 SettingsPanel 嵌成 centralWidget；对外暴露与原来
    FramelessMainWindow._settings 完全相同的信号/方法，
    这样 main.py 的连接代码几乎不用改，只要换一下目标对象。
    """

    closed = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        from .settings_panel import SettingsPanel
        from .widgets import theme

        self.setWindowTitle("TodoSeine · 设置")
        self.resize(640, 680)
        # 同 InfoWindow：显式暗底，不依赖系统主题（浅色主题下控件内全是白字）
        self.setStyleSheet(f"QMainWindow {{ background: {theme.PANEL.name()}; }}")
        self._panel = SettingsPanel(self)
        self.setCentralWidget(self._panel)

    def closeEvent(self, event) -> None:  # noqa: N802
        super().closeEvent(event)
        self.closed.emit()

    # ---------- 对外透传 SettingsPanel 的信号 / 方法 ----------
    # main.py 原来连 window._settings.xxx 的地方，现在连这里

    @property
    def config_saved(self):  # noqa: D401 — 透传属性
        return self._panel.config_saved

    @property
    def sync_requested(self):
        return self._panel.sync_requested

    @property
    def wipe_requested(self):
        return self._panel.wipe_requested

    @property
    def hermes_rotated(self):
        return self._panel.hermes_rotated

    def set_hermes_url(self, url: str) -> None:
        self._panel.set_hermes_url(url)

    def show_and_raise(self) -> None:
        """确保窗口可见且在前台。"""
        self.show()
        self.raise_()
        self.activateWindow()


class InfoWindow(QMainWindow):
    """统计 + 关于的合并独立窗口，QTabWidget 切换。"""

    closed = Signal()

    def __init__(self, db_path: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        from .pages import AboutPage, StatsPage
        from .widgets import theme

        self.setWindowTitle("TodoSeine · 信息")
        self.resize(520, 640)
        # 程序只有暗色一套配色（theme 的注释写明「本程序只有暗色一套实盘」），
        # 这里显式指定底色，避免系统切到浅色主题时出现白底浅字。
        self.setStyleSheet(
            f"QMainWindow, QTabWidget {{ background: {theme.PANEL.name()}; }}"
            "QTabWidget::pane { border: none; top: -1px; }"
            "QTabBar::tab { background: rgba(255,255,255,6);"
            f" color: {theme.SUB.name()}; padding: 7px 16px;"
            " border-top-left-radius: 6px; border-top-right-radius: 6px;"
            " margin-right: 2px; font-size: 12px; }"
            "QTabBar::tab:selected { background: rgba(255,255,255,14);"
            f" color: {theme.TEXT.name()}; }}"
            "QTabBar::tab:hover:!selected { background: rgba(255,255,255,8); }"
        )

        tabs = QTabWidget()
        tabs.setDocumentMode(True)

        self._stats = StatsPage(db_path, self)
        self._about = AboutPage(db_path, self)

        tabs.addTab(self._stats, "统计")
        tabs.addTab(self._about, "关于")
        self.setCentralWidget(tabs)

        # 首次展示时刷新一次统计
        self._stats.refresh()

    def set_db_path(self, db_path: str) -> None:
        self._stats.set_db_path(db_path)
        self._about.set_db_path(db_path)
        self._stats.refresh()

    def closeEvent(self, event) -> None:  # noqa: N802
        super().closeEvent(event)
        self.closed.emit()

    def show_stats(self) -> None:
        self.show_and_raise()
        self.centralWidget().setCurrentIndex(0)
        self._stats.refresh()

    def show_about(self) -> None:
        self.show_and_raise()
        self.centralWidget().setCurrentIndex(1)

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
