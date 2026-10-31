"""截图建待办（Qt 版，替换 tools/snap_todo 的 Tk 实现）。

流程与旧版一致：
  全局热键 Ctrl+Shift+S
    → 全屏半透明遮罩里拖框选区
    → Win11 系统 OCR
    → 确认弹窗（可编辑）
    → 走核心管线入库（去重 / 抽取 / 语义清洗）
    → 托盘气泡 + 看板刷新

截图、遮罩、弹窗全在 Qt 主线程；OCR 与入库放子线程，避免热键按下后界面僵住。
"""

from __future__ import annotations

import logging
import time
from typing import Callable, List, Optional

from PySide6.QtCore import (QBuffer, QIODevice, QObject, QPoint, QRect, Qt,
                            QThread, QTimer, Signal)
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

from .. import config as c2t_config
from ..core import Store, dedupe, extract_todos
from ..models import Message
from .widgets import theme

logger = logging.getLogger("todoseine.qt.snap")


def _normalize(text: str) -> str:
    """WinRT 会在中文字符间插空格（记 得 明 天），去掉 CJK 之间的空白。"""
    import re

    pattern = re.compile(r"(?<=[\u4e00-\u9fff\uff0c\u3002\uff1a\uff1b\uff01\uff1f])\s+"
                         r"(?=[\u4e00-\u9fff])")

    def fix(line: str) -> str:
        out = pattern.sub("", line).strip()
        return out

    return "\n".join(fix(line) for line in (text or "").splitlines()).strip()


# ---------------------------------------------------------------------------
# OCR 工作线程
# ---------------------------------------------------------------------------

class _OcrWorker(QThread):
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, image_bytes: bytes, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._image_bytes = image_bytes

    def run(self) -> None:
        try:
            import io

            from PIL import Image

            from ..sources.uia import UIASourceBackend

            # 全程内存解码：截图 OCR 不落盘，避免敏感画面留在磁盘上
            with Image.open(io.BytesIO(self._image_bytes)) as img:
                text = UIASourceBackend._try_win11_ocr(img.convert("RGB"))
            if not text:
                with Image.open(io.BytesIO(self._image_bytes)) as img2:
                    text = UIASourceBackend._try_tesseract(img2.convert("RGB"))
            self.done.emit(_normalize(text or ""))
        except Exception as exc:  # noqa: BLE001 - OCR 失败要能告诉用户
            logger.warning("截图 OCR 失败：%s", exc)
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 确认弹窗
# ---------------------------------------------------------------------------

class _ConfirmDialog(QDialog):
    def __init__(self, text: str, engine: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("截图待办 · 确认识别结果")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(560, 380)
        self.setStyleSheet(f"QDialog {{ background: {theme.PANEL.name()}; }}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        head = QLabel(f"识别结果（{engine}），可直接编辑后入库：")
        head.setStyleSheet(f"color: {theme.SUB.name()}; font-size: 12px;")
        lay.addWidget(head)

        self.editor = QPlainTextEdit(text)
        self.editor.setStyleSheet(
            f"QPlainTextEdit {{ background: {theme.CARD_BG.name()};"
            f" color: {theme.TEXT.name()}; border: 1px solid {theme.CARD_BORDER.name()};"
            " border-radius: 8px; font-size: 13px; padding: 8px; }"
        )
        lay.addWidget(self.editor, 1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {theme.SUB.name()};"
            " border: none; padding: 6px 14px; font-size: 12px; }")
        cancel.clicked.connect(self.reject)
        bar.addWidget(cancel)

        ok = QPushButton("提取入库")
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setStyleSheet(
            f"QPushButton {{ background: {theme.ACCENT.name()};"
            f" color: {theme.ACCENT_TEXT.name()}; border: none; border-radius: 6px;"
            " padding: 6px 18px; font-size: 12px; font-weight: 600; }")
        ok.clicked.connect(self.accept)
        bar.addWidget(ok)
        lay.addLayout(bar)

    def text(self) -> str:
        return self.editor.toPlainText().strip()


# ---------------------------------------------------------------------------
# 全屏选区遮罩
# ---------------------------------------------------------------------------

class _SelectOverlay(QWidget):
    finished = Signal(object)     # QRect（全局坐标）或 None

    def __init__(self) -> None:
        super().__init__(None)
        self._origin: Optional[QPoint] = None
        self._current: Optional[QPoint] = None
        self._area = QRect()
        for screen in QGuiApplication.screens():
            self._area = self._area.united(screen.geometry())
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(self._area)

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QColor(10, 12, 16, 130))

        if self._origin and self._current:
            rect = QRect(self._origin, self._current).normalized()
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(rect, Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setPen(QPen(theme.ACCENT, 2))
            p.drawRect(rect)

        p.setPen(theme.TEXT)
        p.drawText(self.rect().adjusted(24, 20, -24, -20),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                   "拖动框选要识别的区域　·　右键 / Esc 取消")
        p.end()

    # ---------- 交互 ----------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self.finished.emit(None)
            self.close()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._current = self._origin
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._origin is not None:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._origin is None:
            return
        rect = QRect(self._origin, event.position().toPoint()).normalized()
        self._origin = None
        self.close()
        if rect.width() < 12 or rect.height() < 12:
            self.finished.emit(None)
            return
        global_rect = QRect(rect)
        global_rect.translate(self.geometry().topLeft())
        self.finished.emit(global_rect)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.finished.emit(None)
            self.close()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# 控制器
# ---------------------------------------------------------------------------

class SnapController(QObject):
    """把热键、截图、OCR、入库串起来。"""

    saved = Signal(int, int)     # (提取条数, 实际新增条数)
    failed = Signal(str)

    def __init__(self, db_path_provider: Callable[[], str],
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self._db_path = db_path_provider
        self._overlay: Optional[_SelectOverlay] = None
        self._worker: Optional[_OcrWorker] = None
        self._dialog: Optional[_ConfirmDialog] = None
        self._pending = False      # 已按热键但还没抓屏，挡住连按

    def trigger(self) -> None:
        """热键入口：立刻拉起遮罩，重复按下不叠层。

        选区确认后到抓屏之间隔了 120ms，OCR 期间又有 1~3 秒，
        这两段都不能再拉第二层遮罩（否则后一层会被截进图，识别全是噪声），
        所以除了 overlay/dialog/worker，还要看 _pending 这个「已按下但还没抓屏」的窗口。
        """
        if (self._overlay is not None or self._dialog is not None
                or self._worker is not None or self._pending):
            return
        self._pending = True
        overlay = _SelectOverlay()
        overlay.finished.connect(self._on_selection)
        self._overlay = overlay
        overlay.show()
        overlay.raise_()
        overlay.activateWindow()

    # ---------- 选区 → OCR ----------

    def _on_selection(self, rect) -> None:
        overlay = self._overlay
        self._overlay = None
        self._pending = False
        if overlay is not None:
            overlay.deleteLater()
        if not isinstance(rect, QRect):
            return

        # 让遮罩先消失，再抓屏，否则会把半透明层一起截进去
        QTimer.singleShot(120, lambda: self._capture(rect))

    def _capture(self, rect: QRect) -> None:
        screens = [s for s in QGuiApplication.screens()
                   if s.geometry().intersects(rect)]
        if not screens:
            self.failed.emit("找不到屏幕")
            return
        # 选区可能横跨两块屏（混合 DPI 也一样）：逐屏抓相交部分，再拼回一张图
        dpr = max(s.devicePixelRatio() for s in screens)
        canvas = QPixmap(int(rect.width() * dpr), int(rect.height() * dpr))
        canvas.setDevicePixelRatio(dpr)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        for screen in screens:
            geo = screen.geometry()
            part = rect.intersected(geo)
            if part.isEmpty():
                continue
            shot = screen.grabWindow(0, part.x() - geo.x(), part.y() - geo.y(),
                                     part.width(), part.height())
            if not shot.isNull():
                painter.drawPixmap(part.topLeft() - rect.topLeft(), shot)
        painter.end()
        pixmap = canvas
        if pixmap.isNull():
            self.failed.emit("截图失败")
            return

        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not pixmap.save(buffer, "PNG"):
            buffer.close()
            self.failed.emit("截图编码失败")
            return
        png_bytes = bytes(buffer.data())
        buffer.close()

        # 截图归档是可选功能（默认关闭）：只有用户显式开启才会落盘，
        # 默认走「仅内存 OCR」链路，敏感画面不会留在磁盘上。
        try:
            from ..core import screenshot_archive

            screenshot_archive.save(png_bytes, c2t_config.load(), self._db_path())
        except Exception:  # noqa: BLE001 - 归档失败不能挡住截图建待办
            logger.warning("截图归档写入异常", exc_info=True)

        worker = _OcrWorker(png_bytes, self)
        worker.done.connect(self._on_text)
        worker.failed.connect(self.failed.emit)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _on_text(self, text: str) -> None:
        self._worker = None
        if not text.strip():
            self.failed.emit("没认出文字，换个区域再试")
            return

        dialog = _ConfirmDialog(text, "Windows OCR")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.accepted.connect(lambda: self._ingest(dialog.text()))
        dialog.rejected.connect(lambda: None)
        self._dialog = dialog
        dialog.exec()
        self._dialog = None

    # ---------- 入库 ----------

    def _ingest(self, text: str) -> None:
        if not text:
            return
        db = self._db_path()
        if not db:
            self.failed.emit("尚未配置数据库路径")
            return
        cfg = c2t_config.load()
        now = int(time.time())
        msgs: List[Message] = [
            Message(timestamp=now, sender_id="snap", sender_name="截图",
                    content=line.strip())
            for line in text.splitlines() if line.strip()
        ]
        if not msgs:
            return
        try:
            unique = dedupe(msgs, strict=bool(cfg.get("strict_dedupe")))
            todos = extract_todos(unique, session="截图")
            store = Store(db)
            try:
                store.add_messages(unique, session="截图")
                added = store.add_todos(todos)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("截图入库失败：%s", exc)
            self.failed.emit(f"入库失败：{exc}")
            return
        self.saved.emit(len(todos), added)
