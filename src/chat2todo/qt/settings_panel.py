"""设置面板——嵌入式 QWidget，放进 FramelessMainWindow 的内容区。

四个页签：
  基础   QQ 窗口选择 / OCR 状态 / 启动行为 / 实时抓取预览
  监听   采集开关与间隔 / 通知 / 免打扰与星标
  AI     AI 增强（复核、归并、意图、改写）与接口配置
  外观   看板显示条数 / 圆角 / 材质 / 吸附 / 锁定 / 全局热键

所有配置项都读写同一份 cfg dict，底部「保存配置」一次性写回
~/.config/chat2todo/config.toml 并 emit config_saved。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config as c2t_config
from . import tray
from .widgets import theme


# ---------- 统一控件样式 ----------

_LINE_STYLE = (
    "QLineEdit { background: rgba(255,255,255,12); color: white;"
    " border: 1px solid rgba(255,255,255,30); border-radius: 6px;"
    " padding: 6px 10px; font-size: 12px; }"
)


def _group(title: str) -> tuple:
    box = QGroupBox(title)
    box.setStyleSheet(
        f"QGroupBox {{ color: {theme.SUB.name()}; font-size: 12px;"
        f" border: 1px solid {theme.CARD_BORDER.name()}; border-radius: 10px;"
        " margin-top: 10px; padding-top: 6px; }"
        "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }"
    )
    form = QFormLayout(box)
    form.setContentsMargins(14, 12, 14, 12)
    form.setSpacing(9)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
    return box, form


def _line(text: str = "", placeholder: str = "") -> QLineEdit:
    edit = QLineEdit(text)
    edit.setPlaceholderText(placeholder)
    edit.setStyleSheet(_LINE_STYLE)
    return edit


def _spin(value: int, low: int, high: int, suffix: str = "") -> QSpinBox:
    box = QSpinBox()
    box.setRange(low, high)
    box.setValue(max(low, min(high, int(value))))
    if suffix:
        box.setSuffix(suffix)
    box.setStyleSheet(
        "QSpinBox { background: rgba(255,255,255,12); color: white;"
        " border: 1px solid rgba(255,255,255,30); border-radius: 6px;"
        " padding: 4px 8px; font-size: 12px; }"
    )
    return box


def _check(checked: bool, tip: str = "") -> QCheckBox:
    box = QCheckBox()
    box.setChecked(bool(checked))
    if tip:
        box.setToolTip(tip)
    return box


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet(f"color: {theme.CAPTION.name()}; font-size: 11px;")
    return lab


def _comma_list(values) -> str:
    if isinstance(values, (list, tuple)):
        return ", ".join(str(v) for v in values)
    return str(values or "")


def _parse_list(text: str) -> list:
    """「群号, 群号」或换行分隔 → 去重列表。"""
    parts = []
    for chunk in str(text or "").replace("，", ",").replace("\n", ",").split(","):
        chunk = chunk.strip()
        if chunk and chunk not in parts:
            parts.append(chunk)
    return parts


# ---------------------------------------------------------------------------
# 设置面板
# ---------------------------------------------------------------------------

class SettingsPanel(QWidget):
    """四个页签 + 底部保存栏。"""

    config_saved = Signal(dict)
    sync_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._cfg = c2t_config.load()
        self._qq_hwnd: int = 0
        self._build()

    # ------------------------------------------------------------------
    # 搭建
    # ------------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.setObjectName("SettingsTabs")
        self._tabs.setStyleSheet(
            "QTabWidget::pane { border: none; top: -1px; }"
            "QTabBar::tab { background: rgba(255,255,255,6);"
            f" color: {theme.SUB.name()}; padding: 9px 18px;"
            " border-top-left-radius: 6px; border-top-right-radius: 6px;"
            " margin-right: 2px; font-size: 12px; }"
            "QTabBar::tab:selected { background: rgba(255,255,255,14);"
            f" color: {theme.TEXT.name()}; }}"
            "QTabBar::tab:hover:!selected { background: rgba(255,255,255,8); }"
        )
        self._tabs.addTab(self._build_basic_tab(), "基础")
        self._tabs.addTab(self._build_monitor_tab(), "监听")
        self._tabs.addTab(self._build_ai_tab(), "AI")
        self._tabs.addTab(self._build_appearance_tab(), "外观")
        root.addWidget(self._tabs, 1)

        bar = QHBoxLayout()
        bar.setContentsMargins(16, 6, 16, 10)
        self._status = _hint("")
        bar.addWidget(self._status, 1)
        reload_btn = QPushButton("重新载入")
        reload_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reload_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.CARD_BG.name()}; color: {theme.SUB.name()};"
            f" border: 1px solid {theme.CARD_BORDER.name()}; border-radius: 6px;"
            " padding: 6px 14px; font-size: 12px; }"
        )
        reload_btn.clicked.connect(self._reload)
        bar.addWidget(reload_btn)

        self._save_btn = QPushButton("保存配置")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.ACCENT.name()};"
            f" color: {theme.ACCENT_TEXT.name()}; border: none; border-radius: 6px;"
            " padding: 7px 20px; font-size: 12px; font-weight: 600; }"
            f"QPushButton:hover {{ background: "
            f"{theme.mix(theme.ACCENT, theme.PANEL, 0.15).name()}; }}"
        )
        self._save_btn.clicked.connect(self._save_config)
        bar.addWidget(self._save_btn)
        root.addLayout(bar)

        QTimer.singleShot(0, self._refresh_qq_list)
        QTimer.singleShot(100, self._check_ocr_status)

    # ---------- 基础 ----------

    def _build_basic_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)

        qq_box, qq_form = _group("QQ 窗口")
        self._qq_combo = QComboBox()
        self._qq_combo.setMinimumWidth(300)
        self._qq_combo.setStyleSheet(
            "QComboBox { background: rgba(255,255,255,12); color: white;"
            " border: 1px solid rgba(255,255,255,30); border-radius: 6px;"
            " padding: 6px 10px; font-size: 12px; }"
            "QComboBox QAbstractItemView { background: #1e1e1e; color: white; }"
        )
        qq_row = QHBoxLayout()
        qq_row.addWidget(self._qq_combo, 1)
        refresh_btn = QPushButton("刷新")
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.CARD_BG.name()}; color: {theme.TEXT.name()};"
            f" border: 1px solid {theme.CARD_BORDER.name()}; border-radius: 6px;"
            " padding: 6px 14px; font-size: 12px; }"
        )
        refresh_btn.clicked.connect(self._refresh_qq_list)
        qq_row.addWidget(refresh_btn)
        qq_form.addRow("监听窗口:", qq_row)
        self._qq_status = _hint("")
        qq_form.addRow("", self._qq_status)
        lay.addWidget(qq_box)

        ocr_box, ocr_form = _group("OCR 引擎")
        self._ocr_status = QLabel("检测中…")
        self._ocr_status.setStyleSheet(f"color: {theme.SUB.name()}; font-size: 12px;")
        ocr_form.addRow("Win11 系统 OCR:", self._ocr_status)
        ocr_form.addRow("", _hint("主路径；从截图中识别 QQ 聊天窗口里的消息，不需要登录接口"))
        lay.addWidget(ocr_box)

        boot_box, boot_form = _group("启动行为")
        self._autostart_cb = _check(tray.is_autostart_enabled())
        self._autostart_cb.toggled.connect(self._on_autostart_toggle)
        boot_form.addRow("开机自启:", self._autostart_cb)
        self._topmost_cb = _check(bool(self._cfg.get("always_topmost", False)))
        boot_form.addRow("窗口常驻最上层:", self._topmost_cb)
        lay.addWidget(boot_box)

        preview_box, preview_form = _group("实时消息预览（调试）")
        btn_row = QHBoxLayout()
        fetch_btn = QPushButton("立即抓取并 OCR")
        fetch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fetch_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.ACCENT.name()};"
            f" color: {theme.ACCENT_TEXT.name()}; border: none; border-radius: 6px;"
            " padding: 6px 14px; font-size: 12px; }"
        )
        fetch_btn.clicked.connect(self._do_fetch_now)
        btn_row.addWidget(fetch_btn)
        btn_row.addStretch(1)
        preview_form.addRow(btn_row)

        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setPlaceholderText("点击上方按钮立即抓取当前 QQ 窗口的聊天消息")
        self._preview.setStyleSheet(
            "QPlainTextEdit { background: rgba(0,0,0,40); color: #cbd5e1;"
            " border: 1px solid rgba(255,255,255,20); border-radius: 6px;"
            " font-family: Consolas, monospace; font-size: 12px; }"
        )
        self._preview.setFixedHeight(150)
        preview_form.addRow(self._preview)
        lay.addWidget(preview_box, 1)
        return page

    # ---------- 监听 ----------

    def _build_monitor_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)
        uia = self._cfg.get("uia") or {}

        cap_box, cap_form = _group("采集")
        self._uia_enabled = _check(uia.get("enabled", True))
        cap_form.addRow("启用实时监听:", self._uia_enabled)
        self._interval = _spin(int(uia.get("interval") or 5), 1, 600, " 秒")
        cap_form.addRow("抓取间隔:", self._interval)
        self._session_name = _line(str(uia.get("session") or "QQ实时"),
                                   "OCR 通道读不到群名，用这个名字记录来源")
        cap_form.addRow("来源名称:", self._session_name)
        sync_row = QHBoxLayout()
        sync_btn = QPushButton("立即同步一次")
        sync_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sync_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.CARD_BG.name()}; color: {theme.TEXT.name()};"
            f" border: 1px solid {theme.CARD_BORDER.name()}; border-radius: 6px;"
            " padding: 6px 14px; font-size: 12px; }"
        )
        sync_btn.clicked.connect(self.sync_requested.emit)
        sync_row.addWidget(sync_btn)
        sync_row.addStretch(1)
        cap_form.addRow("", sync_row)
        lay.addWidget(cap_box)

        notify = self._cfg.get("notify") or {}
        n_box, n_form = _group("通知")
        self._notify_new = _check(notify.get("new_todo", True))
        n_form.addRow("新待办提醒:", self._notify_new)
        self._notify_due = _check(notify.get("due", True))
        n_form.addRow("到期提醒:", self._notify_due)
        self._due_within = _spin(int(notify.get("due_within") or 3600), 0, 86400, " 秒")
        n_form.addRow("提前提醒:", self._due_within)
        lay.addWidget(n_box)

        filt = self._cfg.get("filter") or {}
        f_box, f_form = _group("免打扰 / 星标")
        self._ignore_groups = _line(_comma_list(filt.get("ignore_groups")),
                                    "群号或 QQ 号，逗号分隔；这些来源完全不统计")
        f_form.addRow("免打扰:", self._ignore_groups)
        self._star_groups = _line(_comma_list(filt.get("star_groups")),
                                  "群号或 QQ 号，逗号分隔；这些来源标为重点")
        f_form.addRow("特别关心:", self._star_groups)
        f_form.addRow("", _hint("留空表示不启用；星标来源仍参与判定，只是会加上★重点标记"))
        lay.addWidget(f_box)
        lay.addStretch(1)
        return page

    # ---------- AI ----------

    def _build_ai_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)
        ai = self._cfg.get("ai") or {}

        box, form = _group("AI 增强")
        self._ai_enabled = _check(ai.get("enabled", False),
                                  "关闭时下面四项一律不生效")
        form.addRow("启用 AI:", self._ai_enabled)
        self._ai_review = _check(ai.get("review", False), "边界样本二次复核")
        form.addRow("边界复核:", self._ai_review)
        self._ai_merge = _check(ai.get("merge", False), "同一件事的多条消息归并")
        form.addRow("同事实归并:", self._ai_merge)
        self._ai_intent = _check(ai.get("intent", False), "判断说话人是下指令还是在转述")
        form.addRow("说话人意图:", self._ai_intent)
        self._ai_rewrite = _check(ai.get("rewrite", False), "把口语改写成待办句式")
        form.addRow("待办改写:", self._ai_rewrite)
        form.addRow("", _hint("开启后会把相关聊天原文发送到下面的接口，按需谨慎使用"))
        lay.addWidget(box)

        api_box, api_form = _group("接口")
        self._ai_base = _line(str(ai.get("base_url") or ""),
                              "https://api.openai.com/v1")
        api_form.addRow("Base URL:", self._ai_base)
        self._ai_model = _line(str(ai.get("model") or "gpt-4o-mini"))
        api_form.addRow("模型:", self._ai_model)
        self._ai_key = _line(str(ai.get("api_key") or ""), "也可用环境变量 C2T_AI_KEY")
        self._ai_key.setEchoMode(QLineEdit.EchoMode.Password)
        api_form.addRow("API Key:", self._ai_key)
        self._ai_timeout = _spin(int(ai.get("timeout") or 15), 3, 120, " 秒")
        api_form.addRow("超时:", self._ai_timeout)
        lay.addWidget(api_box)
        lay.addStretch(1)
        return page

    # ---------- 外观 ----------

    def _build_appearance_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)
        dash = self._cfg.get("dash") or {}

        view_box, view_form = _group("看板")
        self._top_n = _spin(int(dash.get("top_n") or 5), 0, 100, " 条")
        view_form.addRow("默认显示条数:", self._top_n)
        self._radius = _spin(int(dash.get("radius") or 14), 0, 32, " px")
        view_form.addRow("窗口圆角:", self._radius)
        self._clock_cb = _check(dash.get("clock", True))
        view_form.addRow("显示时钟:", self._clock_cb)
        self._anim_cb = _check(dash.get("anim", True))
        view_form.addRow("交互动画:", self._anim_cb)
        self._material_cb = _check(dash.get("material", True))
        view_form.addRow("系统材质:", self._material_cb)
        self._snap_cb = _check(dash.get("snap", True))
        view_form.addRow("边缘吸附:", self._snap_cb)
        self._lock_cb = _check(dash.get("lock", False))
        view_form.addRow("锁定位置:", self._lock_cb)
        view_form.addRow("", _hint("位置与尺寸会自动记住；改圆角/材质后重开窗口生效"))
        lay.addWidget(view_box)

        hk = self._cfg.get("hotkey") or {}
        hk_box, hk_form = _group("全局热键")
        self._hk_enabled = _check(hk.get("enabled", True))
        hk_form.addRow("启用热键:", self._hk_enabled)
        self._hk_show = _line(str(hk.get("show") or "ctrl+alt+d"), "例如 ctrl+alt+d")
        hk_form.addRow("唤出 / 收起看板:", self._hk_show)
        self._hk_snap = _line(str(hk.get("snap") or "ctrl+shift+s"), "例如 ctrl+shift+s")
        hk_form.addRow("截图建待办:", self._hk_snap)
        hk_form.addRow("", _hint("被其它软件占用时注册会失败，改一个组合再保存"))
        lay.addWidget(hk_box)

        thr_box, thr_form = _group("判定阈值")
        self._todo_thr = _spin(int(self._cfg.get("todo_threshold", 4)), 0, 10)
        thr_form.addRow("待办最低分:", self._todo_thr)
        self._pending_thr = _spin(int(self._cfg.get("pending_threshold", 3)), 0, 10)
        thr_form.addRow("待确认最低分:", self._pending_thr)
        lay.addWidget(thr_box)

        store_box, store_form = _group("数据与日志")
        self._db_path = _line(str(self._cfg.get("db_path") or ""))
        store_form.addRow("数据库路径:", self._db_path)
        self._log_level = QComboBox()
        for level in ("debug", "info", "warning", "error"):
            self._log_level.addItem(level, level)
        idx = self._log_level.findData(str(self._cfg.get("log_level") or "info"))
        if idx >= 0:
            self._log_level.setCurrentIndex(idx)
        self._log_level.setStyleSheet(
            "QComboBox { background: rgba(255,255,255,12); color: white;"
            " border: 1px solid rgba(255,255,255,30); border-radius: 6px;"
            " padding: 5px 10px; font-size: 12px; }"
        )
        store_form.addRow("日志级别:", self._log_level)
        lay.addWidget(store_box)
        lay.addStretch(1)
        return page

    # ------------------------------------------------------------------
    # QQ 窗口枚举 / OCR 检测
    # ------------------------------------------------------------------

    def _refresh_qq_list(self) -> None:
        self._qq_combo.clear()
        wins = tray.enum_qq_windows()
        if not wins:
            self._qq_combo.addItem("（未发现运行中的 QQ 主窗口）", userData=0)
            self._qq_status.setText("请启动 QQ 后点「刷新」")
            return
        saved = int((self._cfg.get("uia") or {}).get("hwnd") or 0)
        for i, (hwnd, title, pid) in enumerate(wins):
            self._qq_combo.addItem(f"PID {pid} — {title} (hwnd={hwnd})", userData=hwnd)
            if hwnd == saved:
                self._qq_combo.setCurrentIndex(i)
        if saved == 0:
            self._qq_combo.setCurrentIndex(0)
        self._qq_hwnd = int(self._qq_combo.currentData() or 0)
        self._qq_combo.currentIndexChanged.connect(self._on_qq_changed)
        self._qq_status.setText(f"已选择 hwnd={self._qq_hwnd}")

    def _on_qq_changed(self, _index: int) -> None:
        self._qq_hwnd = int(self._qq_combo.currentData() or 0)
        self._qq_status.setText(f"已选择 hwnd={self._qq_hwnd}")

    def _check_ocr_status(self) -> None:
        try:
            import winrt.windows.media.ocr as winrt_ocr  # type: ignore

            _ = winrt_ocr.OcrEngine
            self._ocr_status.setText("✓ 就绪（Win11 系统 OCR）")
            self._ocr_status.setStyleSheet("color: #4ade80; font-size: 12px;")
        except Exception as exc:  # noqa: BLE001
            self._ocr_status.setText(f"✗ 不可用：{exc}")
            self._ocr_status.setStyleSheet("color: #ef4444; font-size: 12px;")

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------

    def _on_autostart_toggle(self, checked: bool) -> None:
        if tray.set_autostart(checked):
            return
        self._autostart_cb.blockSignals(True)
        self._autostart_cb.setChecked(not checked)
        self._autostart_cb.blockSignals(False)
        QMessageBox.warning(self, "开机自启", "设置失败（可能需要管理员权限）")

    def _do_fetch_now(self) -> None:
        self._preview.setPlainText("抓取中…")
        QApplication.processEvents()
        try:
            from ..sources.uia import fetch_messages

            msgs = fetch_messages(hwnd=self._qq_hwnd)
            if not msgs:
                self._preview.setPlainText(
                    "（未抓到任何消息）\n请确认 QQ 聊天窗口可见，且已打开聊天面板。")
                return
            self._preview.setPlainText(
                "\n".join(f"[{m.sender_name}] {m.content}" for m in msgs))
        except Exception as exc:  # noqa: BLE001
            self._preview.setPlainText(f"抓取失败：{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # 保存 / 重载
    # ------------------------------------------------------------------

    def _collect(self) -> dict:
        """把界面值收回 cfg（不落盘）。"""
        cfg = dict(self._cfg)
        cfg["always_topmost"] = self._topmost_cb.isChecked()
        cfg["todo_threshold"] = self._todo_thr.value()
        cfg["pending_threshold"] = self._pending_thr.value()
        cfg["db_path"] = self._db_path.text().strip()
        cfg["log_level"] = self._log_level.currentData() or "info"

        cfg["uia"] = {
            "enabled": self._uia_enabled.isChecked(),
            "interval": self._interval.value(),
            "session": self._session_name.text().strip() or "QQ实时",
            "hwnd": self._qq_hwnd,
            "ocr_enabled": True,
        }
        cfg["notify"] = {
            "new_todo": self._notify_new.isChecked(),
            "due": self._notify_due.isChecked(),
            "due_within": self._due_within.value(),
        }
        cfg["filter"] = {
            "ignore_groups": _parse_list(self._ignore_groups.text()),
            "star_groups": _parse_list(self._star_groups.text()),
            "star_senders": list((self._cfg.get("filter") or {}).get("star_senders") or []),
        }
        cfg["ai"] = {
            "enabled": self._ai_enabled.isChecked(),
            "review": self._ai_review.isChecked(),
            "merge": self._ai_merge.isChecked(),
            "intent": self._ai_intent.isChecked(),
            "rewrite": self._ai_rewrite.isChecked(),
            "base_url": self._ai_base.text().strip(),
            "model": self._ai_model.text().strip() or "gpt-4o-mini",
            "api_key": self._ai_key.text().strip(),
            "timeout": self._ai_timeout.value(),
        }
        dash = dict(self._cfg.get("dash") or {})
        dash.update({
            "top_n": self._top_n.value(),
            "radius": self._radius.value(),
            "clock": self._clock_cb.isChecked(),
            "anim": self._anim_cb.isChecked(),
            "material": self._material_cb.isChecked(),
            "snap": self._snap_cb.isChecked(),
            "lock": self._lock_cb.isChecked(),
        })
        cfg["dash"] = dash
        cfg["hotkey"] = {
            "enabled": self._hk_enabled.isChecked(),
            "show": self._hk_show.text().strip() or "ctrl+alt+d",
            "snap": self._hk_snap.text().strip() or "ctrl+shift+s",
        }
        return cfg

    def _save_config(self) -> None:
        cfg = self._collect()
        if not cfg.get("db_path"):
            QMessageBox.warning(self, "配置", "数据库路径不能为空。")
            return
        try:
            c2t_config.save(cfg)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "配置", f"保存失败：{exc}")
            return
        self._cfg = cfg
        self._status.setText("已保存 · 立即生效")
        QTimer.singleShot(2500, lambda: self._status.setText(""))
        self.config_saved.emit(dict(cfg))

    def _reload(self) -> None:
        """从磁盘重新读一遍，把控件值刷成最新。"""
        cfg = c2t_config.load()
        self._cfg = cfg
        ai = cfg.get("ai") or {}
        uia = cfg.get("uia") or {}
        dash = cfg.get("dash") or {}
        notify = cfg.get("notify") or {}
        filt = cfg.get("filter") or {}
        hk = cfg.get("hotkey") or {}

        self._ai_enabled.setChecked(bool(ai.get("enabled")))
        self._ai_review.setChecked(bool(ai.get("review")))
        self._ai_merge.setChecked(bool(ai.get("merge")))
        self._ai_intent.setChecked(bool(ai.get("intent")))
        self._ai_rewrite.setChecked(bool(ai.get("rewrite")))
        self._ai_base.setText(str(ai.get("base_url") or ""))
        self._ai_model.setText(str(ai.get("model") or "gpt-4o-mini"))
        self._ai_key.setText(str(ai.get("api_key") or ""))
        self._ai_timeout.setValue(int(ai.get("timeout") or 15))

        self._uia_enabled.setChecked(bool(uia.get("enabled", True)))
        self._interval.setValue(int(uia.get("interval") or 5))
        self._session_name.setText(str(uia.get("session") or "QQ实时"))

        self._notify_new.setChecked(bool(notify.get("new_todo", True)))
        self._notify_due.setChecked(bool(notify.get("due", True)))
        self._due_within.setValue(int(notify.get("due_within") or 3600))
        self._ignore_groups.setText(_comma_list(filt.get("ignore_groups")))
        self._star_groups.setText(_comma_list(filt.get("star_groups")))

        self._top_n.setValue(int(dash.get("top_n") or 5))
        self._radius.setValue(int(dash.get("radius") or 14))
        self._clock_cb.setChecked(bool(dash.get("clock", True)))
        self._anim_cb.setChecked(bool(dash.get("anim", True)))
        self._material_cb.setChecked(bool(dash.get("material", True)))
        self._snap_cb.setChecked(bool(dash.get("snap", True)))
        self._lock_cb.setChecked(bool(dash.get("lock")))
        self._hk_enabled.setChecked(bool(hk.get("enabled", True)))
        self._hk_show.setText(str(hk.get("show") or "ctrl+alt+d"))
        self._hk_snap.setText(str(hk.get("snap") or "ctrl+shift+s"))

        self._todo_thr.setValue(int(cfg.get("todo_threshold", 4)))
        self._pending_thr.setValue(int(cfg.get("pending_threshold", 3)))
        self._db_path.setText(str(cfg.get("db_path") or ""))
        idx = self._log_level.findData(str(cfg.get("log_level") or "info"))
        if idx >= 0:
            self._log_level.setCurrentIndex(idx)
        self._refresh_qq_list()
        self._status.setText("已重新载入")
        QTimer.singleShot(2000, lambda: self._status.setText(""))
