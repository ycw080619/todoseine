"""PySide6 应用入口。

运行方式：
  python -m chat2todo.qt.main          # 开发模式
  PyInstaller onefile chat2todo        # 打包后直接运行 exe

启动顺序：单实例 → QApplication → 主窗口 → 监听线程 → 托盘 → 热键 → 到期提醒扫描。
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, QCoreApplication, Signal, Qt
from PySide6.QtWidgets import QApplication

from .. import config as c2t_config
from ..core import Store
from ..core.privacy import install_log_filter
from ..core.dash_data import due_remindable
from .hotkey import HotkeyManager
from .hotkey import resolve_specs as resolve_hotkey_specs
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
    # 日志里出现的手机号 / 身份证一律打码；白名单群在这里不生效
    install_log_filter(cfg)


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

    # ---------- 高 DPI：必须在 QApplication 之前设置 ----------
    # Qt6 默认启用缩放，但显式声明更稳；AA_UseHighDpiPixmaps 让图标在高 DPI 下清晰
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

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
        # 看板默认不抢焦点，isActiveWindow() 基本永远为假，所以按「可见即收起」判断
        if window.isVisible():
            window.hide()
            return
        window.show()
        window.reveal()
        window._allow_focus()

    def apply_hotkeys(cfg_now: dict) -> None:
        hotkeys.unbind_all()
        specs = resolve_hotkey_specs(cfg_now)
        if not specs["enabled"]:
            return
        if not hotkeys.bind(specs["show"], on_show_hotkey):
            tray.notify("热键未生效", f"{specs['show']} 可能被其它程序占用")
        if not hotkeys.bind(specs["snap"], snap.trigger):
            tray.notify("热键未生效", f"{specs['snap']} 可能被其它程序占用")

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

    # ---------- Obsidian 同步：变更防抖 3s + 每 60s 拉一次勾选回写 ----------
    obs_debounce = QTimer()
    obs_debounce.setSingleShot(True)
    obs_debounce.setInterval(3000)

    def obsidian_sync(pull: bool = True) -> None:
        log = logging.getLogger("chat2todo.obsidian")
        cfg_now = c2t_config.load()
        if not str(cfg_now.get("obsidian_vault") or ""):
            return
        try:
            store = Store(db_path())
            try:
                from ..core.obsidian import make_sync

                sync = make_sync(cfg_now, store)
                if sync is None:
                    return
                result = sync.sync() if pull else {}
                if not pull:
                    sync.push()
                if result.get("done"):
                    tray.notify("Obsidian 同步", f"回写 {result['done']} 条完成状态")
            finally:
                store.close()
        except Exception:  # noqa: BLE001 - 同步失败不影响主流程
            log.exception("Obsidian 同步失败")

    obs_debounce.timeout.connect(lambda: obsidian_sync(pull=False))
    monitor.todos_added.connect(lambda _items: obs_debounce.start())

    obs_timer = QTimer()
    obs_timer.setInterval(60_000)
    obs_timer.timeout.connect(obsidian_sync)
    obs_timer.start()
    QTimer.singleShot(12_000, obsidian_sync)

    # ---------- 桌面美化联动：脱敏 JSON/ICS + 本地回写接口 ----------
    from ..core.desktop_link import DesktopLink

    class _LinkBridge(QObject):
        """HTTP 线程改完库后发信号，主线程再刷看板 / 防抖导出。"""

        changed = Signal()

    bridge = _LinkBridge()
    link_state: dict = {"link": None}

    def teardown_link() -> None:
        old = link_state.get("link")
        link_state["link"] = None
        if old is not None:
            old.stop_server()

    def schedule_export() -> None:
        link = link_state.get("link")
        if link is not None:
            link.schedule()

    def on_link_changed() -> None:
        window._dashboard.reload(force=True)  # noqa: SLF001
        schedule_export()

    bridge.changed.connect(on_link_changed)

    def setup_link(cfg_now: dict) -> None:
        teardown_link()
        link = DesktopLink(cfg_now, db_path())
        link_state["link"] = link
        if not link.enabled:
            return
        link.on_change = bridge.changed.emit
        url = link.start_server()
        if url:
            tray.notify("桌面美化联动", f"回写接口已启动：{url}")
        else:
            tray.notify("桌面美化联动", "本地接口未启动，仅输出待办文件")
        link.export()                      # 启动先落一次盘，小组件立刻有数据

    setup_link(cfg)
    monitor.todos_added.connect(lambda _items: schedule_export())
    window._dashboard.data_changed.connect(schedule_export)  # noqa: SLF001

    # ---------- Hermes 运维服务：本地 token 鉴权 + 巡检 + 每日汇总 ----------
    from ..core.hermes import HermesService, daily_summary

    class _QtHost:
        """把主程序的能力以最小接口暴露给 Hermes（HTTP 线程里调用）。"""

        def status(self) -> dict:
            cfg_now = c2t_config.load()
            ai = cfg_now.get("ai") or {}
            data: dict = {"running": monitor.running(), "status_text": monitor.status()}
            try:
                store = Store(db_path())
                try:
                    data.update(store.stats())
                finally:
                    store.close()
            except Exception:  # noqa: BLE001 - 库没建好时也要能回状态
                pass
            data["ai_enabled"] = bool(ai.get("enabled"))
            data["ai_configured"] = bool(ai.get("base_url") and ai.get("api_key"))
            data["safe_mode"] = False
            return data

        def restart(self) -> bool:
            monitor.stop()
            monitor.start()
            return monitor.running()

        def snapshot(self):
            link = link_state.get("link")
            return link.snapshot() if link is not None else []

        def summary(self) -> str:
            store = Store(db_path())
            try:
                return daily_summary(store, c2t_config.load())
            finally:
                store.close()

        def apply(self, action: str, payload: dict):
            from ..core.hermes import _apply_on

            store = Store(db_path())
            try:
                return _apply_on(store, action, payload)
            finally:
                store.close()

        def forward_enabled(self) -> bool:
            return bool((c2t_config.load().get("forward") or {}).get("enabled"))

    hermes_state: dict = {"service": None}

    def teardown_hermes() -> None:
        service = hermes_state.get("service")
        hermes_state["service"] = None
        if service is not None:
            service.stop()

    def setup_hermes(cfg_now: dict) -> None:
        teardown_hermes()
        if not (cfg_now.get("hermes") or {}).get("enabled", True):
            return
        service = HermesService(_QtHost(),
                                port=int((cfg_now.get("hermes") or {}).get("port") or 0))
        url = service.start()
        if url:
            hermes_state["service"] = service
            log = logging.getLogger("chat2todo.hermes")
            log.info("Hermes 服务：%s", url)
            window._settings.set_hermes_url(url)  # noqa: SLF001

    setup_hermes(cfg)
    window._settings.hermes_rotated.connect(lambda: setup_hermes(c2t_config.load()))  # noqa: SLF001

    def hermes_patrol() -> None:
        """每 10 分钟巡检：采集线程停了就自动重启，异常只提示不中断。"""
        cfg_now = c2t_config.load()
        hermes_cfg = cfg_now.get("hermes") or {}
        if not hermes_cfg.get("enabled", True):
            return
        if not (cfg_now.get("uia") or {}).get("enabled", True):
            return
        if monitor.running() or monitor.stopping():
            return
        log = logging.getLogger("chat2todo.hermes")
        log.warning("巡检发现采集未运行，尝试自动重启")
        try:
            monitor.start()
            tray.notify("巡检", "采集线程已自动重启")
        except Exception:  # noqa: BLE001
            log.exception("自动重启失败")

    patrol_timer = QTimer()
    patrol_timer.setInterval(10 * 60_000)
    patrol_timer.timeout.connect(hermes_patrol)
    patrol_timer.start()

    def daily_summary_job() -> None:
        """每天固定时间生成汇总，写到导出目录并托盘提示。"""
        cfg_now = c2t_config.load()
        today = datetime.now().strftime("%Y-%m-%d")
        if not (cfg_now.get("hermes") or {}).get("daily_summary", True):
            return
        folder = os.path.join(os.path.dirname(os.path.abspath(db_path())), "summary")
        target = os.path.join(folder, f"{today}.md")
        if os.path.isfile(target):
            return
        try:
            os.makedirs(folder, exist_ok=True)
            store = Store(db_path())
            try:
                text = daily_summary(store, cfg_now)
            finally:
                store.close()
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(text)
            tray.notify("每日汇总", f"已生成 {today}.md")
        except Exception:  # noqa: BLE001 - 汇总失败不影响主流程
            logging.getLogger("chat2todo.hermes").exception("每日汇总失败")

    summary_timer = QTimer()
    summary_timer.setInterval(30 * 60_000)
    summary_timer.timeout.connect(daily_summary_job)
    summary_timer.start()
    QTimer.singleShot(90_000, daily_summary_job)

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
        # 已完成/已删除的条目从集合里摘掉，常驻一整天也不会无限增长
        reminded.intersection_update({i.todo_id for i in items})
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
        setup_link(new_cfg)
        setup_hermes(new_cfg)
        if (new_cfg.get("uia") or {}).get("enabled", True):
            monitor.start()
        else:
            monitor.stop()

    window._settings.config_saved.connect(on_config_saved)  # noqa: SLF001
    window._settings.sync_requested.connect(monitor.sync_now)  # noqa: SLF001

    # ---------- 一键清空：先停采集，再清盘，然后退出重建 ----------
    def on_wipe() -> None:
        from ..core.maintenance import wipe_all

        log = logging.getLogger("chat2todo.qt")
        monitor.stop()
        try:
            removed = wipe_all(c2t_config.load())
        except Exception as exc:  # noqa: BLE001 - 清空失败要让用户看见
            log.exception("一键清空失败")
            tray.notify("清空失败", str(exc)[:120])
            return
        tray.notify("已清空", f"删除 {len(removed)} 项程序数据，程序即将退出")
        log.warning("一键清空完成：%d 项", len(removed))
        QTimer.singleShot(1200, app.quit)

    window._settings.wipe_requested.connect(on_wipe)  # noqa: SLF001

    # ---------- 启动维护：回收站 7 天 + 每日加密备份 ----------
    def startup_maintenance() -> None:
        log = logging.getLogger("chat2todo.qt")
        try:
            store = Store(db_path())
            try:
                dropped = store.purge_trash(7)
            finally:
                store.close()
            if dropped:
                log.info("回收站清理 %d 条（超过 7 天）", dropped)
        except Exception:  # noqa: BLE001 - 库还没建 / 路径不可达时跳过
            pass
        try:
            from ..core.backup import daily_backup

            daily_backup(c2t_config.load())
        except Exception:  # noqa: BLE001 - 备份失败不能挡住启动
            log.exception("每日备份失败")

    QTimer.singleShot(8_000, startup_maintenance)

    # ---------- 首次启动三步引导 ----------
    if not cfg.get("onboarded"):
        from .onboarding import privacy_notice, run_onboarding

        guided = run_onboarding(cfg, window)
        if guided:
            cfg = guided
            db_state["path"] = str(cfg.get("db_path") or db_state["path"])
            try:
                c2t_config.save(cfg, cfg.get("config_file") or None)
            except OSError:
                logging.getLogger("chat2todo.qt").exception("引导结果写入失败")
            monitor.reconfigure(cfg)
            privacy_notice(window)

    # ---------- 启动监听 ----------
    if (cfg.get("uia") or {}).get("enabled", True):
        monitor.start()

    # ---------- 退出 ----------
    # 托盘退出、系统注销、Ctrl+C、热键窗口被强关……都从这里收尾一次
    done = {"v": False}

    def cleanup() -> None:
        if done["v"]:
            return
        done["v"] = True
        try:
            monitor.stop()
        finally:
            hotkeys.unbind_all()
            teardown_link()
            teardown_hermes()
            window._save_geometry()   # noqa: SLF001
            tray.icon.hide()          # 不 hide 会在通知区留一个幽灵图标

    app.aboutToQuit.connect(cleanup)

    def quit_app() -> None:
        app.quit()

    # 控制台里 Ctrl+C 也要能走到 cleanup：Qt 事件循环只在回到解释器时处理信号，
    # 用一个 300ms 的空定时器定期把控制权交回 Python。
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    tick = QTimer()
    tick.timeout.connect(lambda: None)
    tick.start(300)

    tray.quit_requested.connect(quit_app)
    window.minimized_to_tray.connect(lambda: tray.notify(
        "Chat2Todo", "已最小化到托盘，双击图标恢复", 1800))

    if (cfg.get("dash") or {}).get("show_on_start", True):
        window.show()
    else:
        window.hide()
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
