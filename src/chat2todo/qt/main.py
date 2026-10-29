"""PySide6 应用入口。

运行方式：
  python -m chat2todo.qt.main          # 开发模式
  PyInstaller onefile chat2todo        # 打包后直接运行 exe

启动顺序：单实例 → QApplication → 主窗口 → 监听线程 → 托盘 → 热键 → 到期提醒扫描。
"""

from __future__ import annotations

import logging
import os
import sys
import time

from PySide6.QtCore import QTimer, QCoreApplication
from PySide6.QtWidgets import QApplication

from .. import config as c2t_config
from ..core import Store
from ..dashboard.data import due_remindable
from .hotkey import HotkeyManager
from .monitor import Monitor
from .snap import SnapController
from .tray_icon import TrayController


def _setup_logging(cfg: dict) -> None:
    """日志写 ~/.chat2todo/app.log；打包成 exe 后没有控制台也能事后排查。"""
    log_dir = os.path.expanduser("~/.chat2todo")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        return
    level = str(cfg.get("log_level") or "info").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(log_dir, "app.log"), encoding="utf-8"),
            logging.StreamHandler(sys.stderr) if sys.stderr else logging.NullHandler(),
        ],
        force=True,
    )


def _install_crash_hook() -> None:
    """无控制台运行时，未捕获异常至少能落进日志，方便事后定位。"""
    logger = logging.getLogger("chat2todo.qt")

    def hook(exc_type, exc, tb) -> None:
        logger.critical("未捕获异常", exc_info=(exc_type, exc, tb))

    sys.excepthook = hook


def main() -> int:
    # ---------- 日志先就位：后面任何一步崩了都能查到 ----------
    boot_cfg = c2t_config.load()
    _setup_logging(boot_cfg)
    _install_crash_hook()

    # ---------- 单实例检查 ----------
    from .tray import is_another_running

    if is_another_running():
        logging.getLogger("chat2todo.qt").info("已有实例在运行，当前进程退出")
        return 0

    # ---------- Qt 插件路径（PyInstaller onefile） ----------
    if getattr(sys, "frozen", False):
        qt_plugins = os.path.join(getattr(sys, "_MEIPASS", ""), "PySide6", "plugins")
        if os.path.isdir(qt_plugins):
            QCoreApplication.addLibraryPath(qt_plugins)

    app = QApplication(sys.argv)
    app.setApplicationName("Chat2Todo")
    app.setQuitOnLastWindowClosed(False)

    cfg = c2t_config.load()

    # ---------- 主窗口 ----------
    from .main_window import FramelessMainWindow

    window = FramelessMainWindow()
    db_state = {"path": str(cfg.get("db_path") or "")}
    notify_state = dict(cfg.get("notify") or {})

    # ---------- 监听 ----------
    monitor = Monitor(db_state["path"], cfg, parent=window)

    def db_path() -> str:
        return db_state["path"]

    # ---------- 托盘 ----------
    tray = TrayController(window, monitor, db_path, parent=window)

    # ---------- 截图建待办 ----------
    snap = SnapController(db_path, parent=window)

    def on_snap_saved(total: int, added: int) -> None:
        window.notify_new_todos([])
        tray.notify("截图待办", f"新增 {added} 条（本次提取 {total} 条）")

    snap.saved.connect(on_snap_saved)
    snap.failed.connect(lambda text: tray.notify("截图失败", text))

    # ---------- 热键 ----------
    hotkeys = HotkeyManager()

    def on_show_hotkey() -> None:
        if window.isVisible() and window.isActiveWindow():
            window.hide()
            return
        window.show()
        window.raise_()
        window.activateWindow()

    def apply_hotkeys(cfg_now: dict) -> None:
        hotkeys.unbind_all()
        hk = cfg_now.get("hotkey") or {}
        if not hk.get("enabled", True):
            return
        if not hotkeys.bind(str(hk.get("show") or "ctrl+alt+d"), on_show_hotkey):
            tray.notify("热键未生效", f"{hk.get('show')} 可能被其它程序占用")
        if not hotkeys.bind(str(hk.get("snap") or "ctrl+shift+s"), snap.trigger):
            tray.notify("热键未生效", f"{hk.get('snap')} 可能被其它程序占用")

    apply_hotkeys(cfg)

    # ---------- 监听 → 界面 ----------
    def on_new_todos(items) -> None:
        if not notify_state.get("new_todo", True):
            return
        if window.isVisible() and not window.is_mini():
            window.notify_new_todos(items)
        else:
            window._dashboard.reload(force=True)  # noqa: SLF001 - 内部刷新即可

    monitor.todos_added.connect(on_new_todos)
    monitor.status_changed.connect(
        lambda text: window.set_mini_summary(f"Chat2Todo · {text}"))

    # ---------- 到期提醒 ----------
    reminded: set = set()

    def scan_due() -> None:
        if not notify_state.get("due", True):
            return
        within = int(notify_state.get("due_within") or 3600)
        try:
            store = Store(db_path())
            try:
                items = store.list_todos("pending")
            finally:
                store.close()
        except Exception:  # noqa: BLE001 - 库还没建/没配路径时静默跳过
            return
        try:
            pairs = [(i, d) for i, d in due_remindable(items, within=within)
                     if i.todo_id not in reminded]
        except Exception:  # noqa: BLE001
            return
        if not pairs:
            return
        reminded.update(i.todo_id for i, _ in pairs)
        if window.isVisible() and not window.is_mini():
            window._dashboard.remind_due(pairs)  # noqa: SLF001
        else:
            first = (pairs[0][0].text or "").replace("\n", " ")
            tray.notify("⏰ 到期提醒",
                        first[:60] if len(pairs) == 1 else f"{len(pairs)} 条待办即将到期")

    due_timer = QTimer()
    due_timer.setInterval(60_000)
    due_timer.timeout.connect(scan_due)
    due_timer.start()
    QTimer.singleShot(20_000, scan_due)

    # ---------- 设置保存 ----------
    def on_config_saved(new_cfg: dict) -> None:
        nonlocal notify_state
        db_state["path"] = str(new_cfg.get("db_path") or db_state["path"])
        notify_state = dict(new_cfg.get("notify") or {})
        logging.getLogger().setLevel(
            getattr(logging, str(new_cfg.get("log_level") or "info").upper(),
                    logging.INFO))
        monitor.reconfigure(new_cfg)
        apply_hotkeys(new_cfg)
        if (new_cfg.get("uia") or {}).get("enabled", True):
            monitor.start()
        else:
            monitor.stop()

    window._settings.config_saved.connect(on_config_saved)  # noqa: SLF001
    window._settings.sync_requested.connect(monitor.sync_now)  # noqa: SLF001

    # ---------- 启动监听 ----------
    if (cfg.get("uia") or {}).get("enabled", True):
        monitor.start()

    # ---------- 退出 ----------
    def quit_app() -> None:
        monitor.stop()
        hotkeys.unbind_all()
        window._save_geometry()  # noqa: SLF001
        app.quit()

    tray.quit_requested.connect(quit_app)
    window.minimized_to_tray.connect(lambda: tray.notify(
        "Chat2Todo", "已最小化到托盘，双击图标恢复", 1800))

    window.show()
    logging.getLogger("chat2todo.qt").info(
        "启动完成 · db=%s · 监听=%s · %s", db_state["path"], monitor.status(),
        time.strftime("%Y-%m-%d %H:%M:%S"))
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:  # noqa: BLE001 - 兜底落盘，窗口版看不到 traceback
        logging.getLogger("chat2todo.qt").exception("启动失败")
        raise
