"""首次启动三步引导（PySide6 模态向导）。

对应文档「基础版·3步启动」：
  1. 下拉选择要监控的 QQ 窗口（自动过滤非 QQ 进程，QQ 未启动时明确提示）
  2. 确认归档位置与默认脱敏规则
  3. 点击「开始监控」

完成后写回 cfg（uia.hwnd / uia.enabled / privacy.enabled / onboarded），
再由主程序弹一次「归档默认脱敏」说明——这两处都是文档写死的交互。
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout,
                               QLabel, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

from ..core.archive import default_archive_dir
from . import tray
from .widgets import theme

_STEPS = ("选择 QQ 窗口", "归档与脱敏", "开始监控")


class _Page(QWidget):
    def __init__(self, title: str, subtitle: str):
        super().__init__()
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(4, 4, 4, 4)
        self.layout_.setSpacing(10)
        head = QLabel(title)
        head.setStyleSheet(f"color: {theme.TEXT.name()}; font-size: 16px; font-weight: 600;")
        self.layout_.addWidget(head)
        sub = QLabel(subtitle)
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {theme.SUB.name()}; font-size: 12px;")
        self.layout_.addWidget(sub)


class OnboardingWizard(QDialog):
    """返回 Accepted 时会把结果写进 self.result_cfg。"""

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._hwnd = 0
        self.result_cfg: Optional[dict] = None
        self.setWindowTitle("Chat2Todo · 三步上手")
        self.setMinimumWidth(520)
        self.setStyleSheet(
            f"QDialog {{ background: {theme.PANEL.name()}; }}"
            f"QLabel {{ color: {theme.TEXT.name()}; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(14)

        self._banner = QLabel("开箱即用 · 零依赖 · AI / Obsidian / 桌面美化 / 插件均为可选 · 仅适配 Windows 11")
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet(
            f"color: {theme.CAPTION.name()}; font-size: 11px;"
            f" background: {theme.CARD_BG.name()}; border-radius: 8px; padding: 8px 10px;")
        root.addWidget(self._banner)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._page_qq())
        self._stack.addWidget(self._page_privacy())
        self._stack.addWidget(self._page_finish())
        root.addWidget(self._stack, 1)

        nav = QHBoxLayout()
        self._step_label = QLabel("")
        self._step_label.setStyleSheet(f"color: {theme.SUB.name()}; font-size: 11px;")
        nav.addWidget(self._step_label, 1)
        self._back_btn = QPushButton("上一步")
        self._back_btn.clicked.connect(self._back)
        nav.addWidget(self._back_btn)
        self._next_btn = QPushButton("下一步")
        self._next_btn.clicked.connect(self._next)
        nav.addWidget(self._next_btn)
        root.addLayout(nav)

        self._page = 0
        self._sync_nav()

    # ---------- 页 1：选择 QQ 窗口 ----------

    def _page_qq(self) -> QWidget:
        page = _Page("第 1 步 · 选择要监控的 QQ 窗口",
                     "只读取窗口上显示的内容，不注入、不修改 QQ 进程。"
                     "QQ 多开时请按 PID / 窗口标题选择对应账号。")
        self._qq_combo = QComboBox()
        page.layout_.addWidget(self._qq_combo)
        row = QHBoxLayout()
        refresh = QPushButton("刷新窗口列表")
        refresh.clicked.connect(self._refresh_qq)
        row.addWidget(refresh)
        row.addStretch(1)
        page.layout_.addLayout(row)
        self._qq_status = QLabel("")
        self._qq_status.setWordWrap(True)
        self._qq_status.setStyleSheet(f"color: {theme.WARN.name()}; font-size: 11px;")
        page.layout_.addWidget(self._qq_status)
        page.layout_.addStretch(1)
        self._refresh_qq()
        return page

    def _refresh_qq(self) -> None:
        from ..sources.uia import qq_window_info

        self._qq_combo.clear()
        wins = tray.enum_qq_windows()
        if not wins:
            self._qq_combo.addItem("（未发现运行中的 QQ 主窗口）", userData=0)
            self._qq_status.setText(
                "QQ 未启动：请先打开 QQ 并登录，再点「刷新窗口列表」。"
                "也可以先完成引导，稍后在「基础」页选择窗口。")
            return
        saved = int((self._cfg.get("uia") or {}).get("hwnd") or 0)
        for i, (hwnd, title, pid) in enumerate(wins):
            info = qq_window_info(hwnd)
            version = info.get("version") or "版本未知"
            self._qq_combo.addItem(
                f"PID {pid} — {title} · QQ {version} (hwnd={hwnd})", userData=hwnd)
            if hwnd == saved:
                self._qq_combo.setCurrentIndex(i)
        self._qq_status.setText("")

    # ---------- 页 2：归档与脱敏 ----------

    def _page_privacy(self) -> QWidget:
        page = _Page("第 2 步 · 归档位置与隐私脱敏",
                     "消息会自动归档为 Markdown（群名/年/月/日.md），"
                     "待办存入本地 SQLite。两者都只保存在这台电脑上。")
        db_path = str(self._cfg.get("db_path") or "")
        archive = default_archive_dir(db_path) or "（未设置数据库路径）"
        info = QLabel(
            f"数据库：{db_path}\n归档目录：{archive}\n\n"
            "默认对手机号 / 身份证 / 银行卡 / 邮箱打码，命中位置标注「已脱敏」。"
            "脱敏只影响归档、日志、看板展示与云端 AI 请求，数据库仍保留原文。")
        info.setWordWrap(True)
        info.setStyleSheet(
            f"color: {theme.SUB.name()}; font-size: 12px;"
            f" background: {theme.CARD_BG.name()}; border-radius: 8px; padding: 10px;")
        page.layout_.addWidget(info)

        self._privacy_cb = QCheckBox("启用归档脱敏（推荐）")
        self._privacy_cb.setChecked(bool((self._cfg.get("privacy") or {}).get("enabled", True)))
        page.layout_.addWidget(self._privacy_cb)
        page.layout_.addWidget(QLabel("白名单群、自定义打码规则可在「设置 → 隐私」里调整。"))
        page.layout_.addStretch(1)
        return page

    # ---------- 页 3：完成 ----------

    def _page_finish(self) -> QWidget:
        page = _Page("第 3 步 · 准备就绪",
                     "完成引导后会把配置写回本地，不会上传任何信息。")
        self._start_cb = QCheckBox("立即开始监控")
        self._start_cb.setChecked(True)
        page.layout_.addWidget(self._start_cb)
        self._dash_cb = QCheckBox("显示桌面悬浮待办看板")
        self._dash_cb.setChecked(True)
        page.layout_.addWidget(self._dash_cb)
        tip = QLabel("提示：随时可用 Ctrl+Alt+D 唤出看板，Ctrl+Shift+S 截图建待办。")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color: {theme.CAPTION.name()}; font-size: 11px;")
        page.layout_.addWidget(tip)
        page.layout_.addStretch(1)
        return page

    # ---------- 导航 ----------

    def _sync_nav(self) -> None:
        self._step_label.setText(
            f"{self._page + 1} / {len(_STEPS)} · {_STEPS[self._page]}")
        self._back_btn.setEnabled(self._page > 0)
        self._next_btn.setText("完成" if self._page == len(_STEPS) - 1 else "下一步")

    def _back(self) -> None:
        if self._page > 0:
            self._page -= 1
            self._stack.setCurrentIndex(self._page)
            self._sync_nav()

    def _next(self) -> None:
        if self._page == 0:
            self._hwnd = int(self._qq_combo.currentData() or 0)
        if self._page < len(_STEPS) - 1:
            self._page += 1
            self._stack.setCurrentIndex(self._page)
            self._sync_nav()
            return
        self._finish()

    def _finish(self) -> None:
        cfg = dict(self._cfg)
        uia = dict(cfg.get("uia") or {})
        uia["hwnd"] = int(self._qq_combo.currentData() or 0)
        uia["enabled"] = self._start_cb.isChecked() and uia.get("enabled", True)
        cfg["uia"] = uia
        privacy = dict(cfg.get("privacy") or {})
        privacy["enabled"] = self._privacy_cb.isChecked()
        cfg["privacy"] = privacy
        dash = dict(cfg.get("dash") or {})
        dash["show_on_start"] = self._dash_cb.isChecked()
        cfg["dash"] = dash
        cfg["onboarded"] = True
        self.result_cfg = cfg
        self.accept()


def run_onboarding(cfg: dict, parent=None) -> Optional[dict]:
    """跑一次引导；用户关掉窗口返回 None（下次启动再引导）。"""
    wizard = OnboardingWizard(cfg, parent)
    if wizard.exec() != QDialog.DialogCode.Accepted:
        return None
    return wizard.result_cfg


def privacy_notice(parent=None) -> None:
    """引导完成后的脱敏说明弹窗（文档要求首次走完流程后提示一次）。"""
    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox(parent)
    box.setWindowTitle("隐私说明")
    box.setIcon(QMessageBox.Icon.Information)
    box.setText("归档默认对敏感信息脱敏")
    box.setInformativeText(
        "手机号 / 身份证 / 银行卡 / 邮箱在归档、日志、看板与云端 AI 请求中会自动打码。\n\n"
        "如果某些群需要保留完整内容，可在「设置 → 隐私」里加入白名单；"
        "白名单只影响归档与看板展示，云端 AI 调用前仍会强制脱敏。\n\n"
        "所有数据都保存在本机，程序不上传任何用户信息。")
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()


def config_home() -> str:
    return os.path.expanduser("~/.chat2todo")
