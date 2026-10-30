"""半自动转发预览窗（高风险功能，仅在 ``forward.enabled`` 打开时可用）。

流程：选中内容 → 预览窗（目标 + 最终文本）→ 3 秒倒计时（发送键置灰）
→ 用户手动点确认 → 关闭窗口把焦点交还目标窗口 → 模拟按键输入 → 审计留痕。

安全约束（对齐开发定稿）：
  * 默认关闭，入口在主界面隐藏；
  * 单次手动触发，无批量 / 定时 / 自动重试；
  * 倒计时期间切换到其它窗口立即取消；
  * 审计只记目标、内容哈希、长度与结果。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QVBoxLayout,
                               QWidget)

from .. import config as c2t_config
from ..core import forward
from .widgets import theme

log = logging.getLogger("chat2todo.qt.forward")


class ForwardDialog(QDialog):
    """预览 + 倒计时 + 手动确认。"""

    def __init__(self, text: str, target: str = "",
                 cfg: Optional[Dict[str, Any]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("半自动转发 · 发送前确认")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(560, 400)
        self.setStyleSheet(f"QDialog {{ background: {theme.PANEL.name()}; }}")

        self._cfg = cfg or c2t_config.load()
        self._target = target or "当前聊天窗口"
        self._remaining = forward.CONFIRM_SECONDS
        self._cancelled_by_focus = False
        self.outcome = "cancelled"      # sent / failed / cancelled

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        head = QLabel(f"目标：{self._target}　（发送后请勿再手动改动输入框）")
        head.setWordWrap(True)
        head.setStyleSheet(f"color: {theme.SUB.name()}; font-size: 12px;")
        lay.addWidget(head)

        self.editor = QPlainTextEdit(forward.prepare(text))
        self.editor.setStyleSheet(
            f"QPlainTextEdit {{ background: {theme.CARD_BG.name()};"
            f" color: {theme.TEXT.name()}; border: 1px solid {theme.CARD_BORDER.name()};"
            " border-radius: 8px; font-size: 13px; padding: 8px; }"
        )
        lay.addWidget(self.editor, 1)

        row = QHBoxLayout()
        self.rewrite_box = QCheckBox("发送前用 AI 改写口吻")
        self.rewrite_box.setStyleSheet(f"color: {theme.SUB.name()}; font-size: 12px;")
        self.rewrite_box.toggled.connect(self._on_rewrite)
        row.addWidget(self.rewrite_box)
        row.addStretch(1)
        self.hint = QLabel("")
        self.hint.setStyleSheet(f"color: {theme.SUB.name()}; font-size: 11px;")
        row.addWidget(self.hint)
        lay.addLayout(row)

        bar = QHBoxLayout()
        bar.addStretch(1)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {theme.SUB.name()};"
            " border: none; padding: 6px 14px; font-size: 12px; }")
        self.cancel_btn.clicked.connect(self.reject)
        bar.addWidget(self.cancel_btn)

        self.send_btn = QPushButton()
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.setEnabled(False)      # 倒计时期间置灰
        self.send_btn.clicked.connect(self._confirm)
        bar.addWidget(self.send_btn)
        lay.addLayout(bar)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._render_button()

        forward.record(self._target, self.editor.toPlainText(), "previewed")

    # -- 倒计时 ---------------------------------------------------------
    def _tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self._timer.stop()
        self._render_button()

    def _render_button(self) -> None:
        if self._remaining > 0:
            self.send_btn.setText(f"确认发送（{self._remaining}s）")
            self.send_btn.setEnabled(False)
            self.send_btn.setStyleSheet(
                f"QPushButton {{ background: {theme.CARD_BG.name()};"
                f" color: {theme.SUB.name()}; border: 1px solid {theme.CARD_BORDER.name()};"
                " border-radius: 6px; padding: 6px 18px; font-size: 12px; }")
        else:
            self.send_btn.setText("确认发送")
            self.send_btn.setEnabled(True)
            self.send_btn.setStyleSheet(
                f"QPushButton {{ background: {theme.ACCENT.name()};"
                f" color: {theme.ACCENT_TEXT.name()}; border: none; border-radius: 6px;"
                " padding: 6px 18px; font-size: 12px; font-weight: 600; }")

    # -- 焦点切换即取消 -------------------------------------------------
    def event(self, ev: QEvent) -> bool:
        if ev.type() == QEvent.Type.WindowDeactivate and self.outcome != "sent":
            if not self._cancelled_by_focus:
                # 文档要求：倒计时期间切窗自动取消，避免误发到别的窗口
                self._cancelled_by_focus = True
                self.outcome = "cancelled"
                self.reject()
                return True
        return super().event(ev)

    # -- AI 改写 ---------------------------------------------------------
    def _on_rewrite(self, checked: bool) -> None:
        if not checked:
            return
        origin = self.editor.toPlainText()
        self.hint.setText("改写中…")
        rewritten = forward.rewrite(origin, self._cfg)
        if rewritten:
            self.editor.setPlainText(rewritten)
            self.hint.setText("已改写")
        else:
            self.rewrite_box.setChecked(False)
            self.hint.setText("AI 不可用，保留原文")

    # -- 确认 -----------------------------------------------------------
    def _confirm(self) -> None:
        if self._remaining > 0:
            return
        self.outcome = "sent"
        self._timer.stop()
        self.accept()

    def payload(self) -> str:
        return forward.prepare(self.editor.toPlainText())


def run(text: str, target: str = "", cfg: Optional[Dict[str, Any]] = None,
        parent: Optional[QWidget] = None) -> bool:
    """弹预览窗；用户确认后异步注入按键。返回是否真的发起发送。"""
    cfg = cfg or c2t_config.load()
    if not forward.enabled(cfg):
        return False
    payload = forward.prepare(text)
    if not payload:
        return False
    dlg = ForwardDialog(payload, target, cfg, parent)
    if dlg.exec() != QDialog.DialogCode.Accepted or dlg.outcome != "sent":
        forward.record(target, payload, "cancelled")
        return False
    final = dlg.payload()
    if not final:
        forward.record(target, payload, "cancelled", error="内容为空")
        return False

    def _send() -> None:
        ok = forward.simulate_send(final)
        forward.record(target, final, "sent" if ok else "failed",
                       error="" if ok else "模拟按键失败")

    # 窗口关闭后焦点回到目标窗口，等焦点切换完成再打字
    QTimer.singleShot(300, _send)
    return True
