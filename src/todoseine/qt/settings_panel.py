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

from PySide6.QtCore import QPointF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config as c2t_config
from ..core import Store
from ..core import keybox
from . import tray
from .widgets import focus_ring, theme


# ---------- 统一控件样式 ----------

_LINE_STYLE = (
    "QLineEdit { background: rgba(255,255,255,10);"
    f" color: {theme.TEXT.name()};"
    f" border: 1px solid {theme.css(theme.LINE_HAIRLINE)}; border-radius: 7px;"
    " padding: 6px 10px; font-size: 12px; }"
    # 聚焦只换边框颜色：宽度恒 1px，控件尺寸不变，不会出现生硬描边
    "QLineEdit:focus {"
    f" border-color: {theme.css(theme.ACCENT)};"
    " background: rgba(255,255,255,14); }"
)

# 下拉框：1px 细线 + 展开只换颜色（宽度恒定，不产生位移）；弹出列表同步令牌底色
_COMBO_STYLE = (
    "QComboBox { background: rgba(255,255,255,10);"
    f" color: {theme.TEXT.name()};"
    f" border: 1px solid {theme.css(theme.LINE_HAIRLINE)}; border-radius: 7px;"
    " padding: 5px 10px; font-size: 12px; }"
    "QComboBox:hover { border-color: rgba(255,255,255,26); }"
    f"QComboBox:focus, QComboBox:on {{ border-color: {theme.css(theme.ACCENT)}; }}"
    # 默认下拉箭头是深色，在深色底上看不见；用 border 三角重绘一个浅色箭头。
    # 注意：Qt 的 QSS 在 ::down-arrow 上并不可靠地支持 border 三角，
    # 所以这里只保留下拉区域宽度，真正的三角由 _Combo.paintEvent 自绘。
    "QComboBox::drop-down { width: 24px; border: none;"
    " subcontrol-position: center right; }"
    "QComboBox::down-arrow { image: none; width: 0; height: 0; }"
    "QComboBox QAbstractItemView {"
    f" background: {theme.CARD_BG.name()};"
    f" color: {theme.TEXT.name()};"
    f" border: 1px solid {theme.css(theme.LINE_HAIRLINE)};"
    " outline: none; padding: 4px;"
    f" selection-background-color: {theme.css(theme.ACCENT_SOFT)};"
    f" selection-color: {theme.TEXT.name()}; }}"
)

# 勾选框：自绘 14px 方点，选中用低饱和主色填充，不引入图片资源
_CHECK_STYLE = (
    f"QCheckBox {{ color: {theme.SUB.name()}; font-size: 12px; spacing: 8px; }}"
    "QCheckBox::indicator { width: 14px; height: 14px; border-radius: 4px;"
    f" border: 1px solid {theme.css(theme.LINE_SOFT)};"
    " background: rgba(255,255,255,10); }"
    "QCheckBox::indicator:hover { border-color: rgba(255,255,255,40); }"
    "QCheckBox::indicator:checked {"
    f" background: {theme.ACCENT.name()};"
    f" border-color: {theme.ACCENT.name()}; }}"
)


class _Combo(QComboBox):
    """下拉框：补画一个浅色三角。

    Qt 的 QSS 在 ::down-arrow 上不保证渲染 border 三角，系统默认箭头
    又是深色的（深色底上看不见），所以这里在 paintEvent 里自绘。
    滚轮行为同 _FocusSpinBox：只有鼠标点击过后才切换选项。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._wheel_active = False

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._wheel_active = True
        super().mousePressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._wheel_active = False
        super().focusOutEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.SUB)

        w, h = 8.0, 4.5
        x = float(self.width() - 16)
        y = (self.height() - h) / 2.0
        path = QPainterPath()
        path.moveTo(QPointF(x, y))
        path.lineTo(QPointF(x + w, y))
        path.lineTo(QPointF(x + w / 2.0, y + h))
        path.closeSubpath()
        painter.drawPath(path)
        painter.end()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._wheel_active:
            super().wheelEvent(event)
        else:
            event.ignore()


class _ProbeWorker(QThread):
    """连接测试放后台跑，避免 15 秒超时把界面冻住。"""

    done = Signal(bool, str)

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int,
                 temperature: float, top_p: float, parent=None):
        super().__init__(parent)
        self._args = (base_url, api_key, model, timeout, temperature, top_p)

    def run(self) -> None:  # noqa: D401 - QThread 入口
        from ..ai import probe

        try:
            ok, msg = probe(*self._args)
        except Exception as exc:  # noqa: BLE001 - 测试失败也要回话
            ok, msg = False, f"测试异常：{type(exc).__name__}: {exc}"
        self.done.emit(ok, msg)


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
    # 外环柔光：挂在控件属性上，防止 Python 侧回收后只剩内环
    edit._focus_glow = focus_ring.attach(edit)  # noqa: SLF001 - 只做生命周期锚点
    return edit


class _FocusSpinBox(QSpinBox):
    """只在被鼠标点击后才响应滚轮的数值框。

    默认 QSpinBox 鼠标悬浮时滚轮就改值，滚动页面经过时极易误触。
    用 _wheel_active 标志位：鼠标按下时激活滚轮，失焦时取消。
    这样只有「点过这个框」之后滚轮才改值，点别处就失效。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._wheel_active = False

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._wheel_active = True
        super().mousePressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._wheel_active = False
        super().focusOutEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._wheel_active:
            super().wheelEvent(event)
        else:
            event.ignore()


class _FocusDoubleSpinBox(QDoubleSpinBox):
    """浮点数版本，行为同 _FocusSpinBox。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._wheel_active = False

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._wheel_active = True
        super().mousePressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._wheel_active = False
        super().focusOutEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._wheel_active:
            super().wheelEvent(event)
        else:
            event.ignore()


def _spin(value: int, low: int, high: int, suffix: str = "") -> QSpinBox:
    box = _FocusSpinBox()
    box.setRange(low, high)
    box.setValue(max(low, min(high, int(value))))
    if suffix:
        box.setSuffix(suffix)
    box.setStyleSheet(
        "QSpinBox { background: rgba(255,255,255,10);"
        f" color: {theme.TEXT.name()};"
        f" border: 1px solid {theme.css(theme.LINE_HAIRLINE)}; border-radius: 7px;"
        " padding: 4px 8px; font-size: 12px; }"
        "QSpinBox:focus {"
        f" border-color: {theme.css(theme.ACCENT)}; }}"
    )
    return box


def _check(checked: bool, tip: str = "") -> QCheckBox:
    box = QCheckBox()
    box.setStyleSheet(_CHECK_STYLE)
    box.setChecked(bool(checked))
    if tip:
        box.setToolTip(tip)
    return box


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet(f"color: {theme.CAPTION.name()}; font-size: 11px;")
    return lab


def _material_from_raw(raw) -> str:
    """兼容旧配置：bool / str 都转成 str。

    旧版 dash.material 是 bool（True=亚克力, False=关闭）；
    新版是 str："off" / "acrylic" / "mica" / "mica_alt" / "tabbed" / "blur"。
    """
    if isinstance(raw, bool):
        return "acrylic" if raw else "off"
    s = str(raw or "").strip().lower()
    return s if s in {"off", "acrylic", "mica", "mica_alt", "tabbed", "blur"} else "acrylic"


def _wrap_scroll(page: QWidget) -> QScrollArea:
    """把页签内容套进滚动区。

    外观页自然高度可达 1300px 以上，直接塞进 QTabWidget 会把整个设置窗口
    顶到超出屏幕，底部「保存配置」按钮就点不到了。套滚动区后窗口保持
    固定高度，内容自己的滚动条负责溢出部分。
    """
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    # 滚动条加宽到 10px 并提亮把手：8px 的暗色滚条几乎看不见，
    # 用户不知道页签里还有内容可以往下滚。
    scroll.setStyleSheet(
        "QScrollArea { background: transparent; border: none; }"
        "QScrollArea > QWidget > QWidget { background: transparent; }"
        "QScrollBar:vertical { background: transparent; width: 10px;"
        " margin: 0; }"
        "QScrollBar::handle:vertical { background: rgba(255,255,255,48);"
        " min-height: 30px; border-radius: 5px; }"
        "QScrollBar::handle:vertical:hover { background: rgba(255,255,255,90); }"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
        " { height: 0; background: transparent; }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
        " { background: transparent; }"
    )
    scroll.setWidget(page)
    return scroll


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
# 群标签列表：每个群号显示成群名 + × 删除按钮，底部输入框可追加
# ---------------------------------------------------------------------------

class GroupTagList(QWidget):
    """免打扰 / 特别关心 的群号列表。

    把原来「逗号分隔的一串群号」改成可视化标签：
      每个标签 = 群名（查不到则显示群号）+ (群号) + × 删除
      底部有输入框 + 添加按钮，输入群号回车或点添加即可追加。
    """

    def __init__(self, values=None, chat_names: dict | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._chat_names = chat_names or {}
        # 群名 → [所有关联 ID]：同一群有「群号」和「内部代码」两个 ID，
        # 展示时优先显示群号（用户认识的那个），过滤值仍用内部代码。
        self._name_to_ids: dict[str, list[str]] = {}
        self._rebuild_name_index()
        self._values: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        self._flow = QVBoxLayout()
        self._flow.setSpacing(4)
        root.addLayout(self._flow)

        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self._input = QLineEdit()
        self._input.setPlaceholderText("输入群号或 QQ 号，回车添加")
        self._input.setStyleSheet(_LINE_STYLE)
        self._input.returnPressed.connect(self._add_current)
        add_row.addWidget(self._input, 1)
        self._add_btn = QPushButton("添加")
        self._add_btn.setFixedHeight(26)
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._add_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.ACCENT.name()};"
            f" color: {theme.ACCENT_TEXT.name()}; border: none; border-radius: 8px;"
            " font-size: 11px; padding: 0 12px; }"
            f"QPushButton:hover {{ background: {theme.mix(theme.ACCENT, theme.PANEL, 0.2).name()}; }}"
        )
        self._add_btn.clicked.connect(self._add_current)
        add_row.addWidget(self._add_btn)
        root.addLayout(add_row)

        self.set_values(values or [])

    # ---------- 数据 ----------

    def set_values(self, values) -> None:
        """重建标签列表。"""
        # 清空旧标签
        while self._flow.count():
            item = self._flow.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._values = []
        for v in values or []:
            s = str(v).strip()
            if s and s not in self._values:
                self._values.append(s)
                self._flow.addWidget(self._make_tag(s))

    def values(self) -> list[str]:
        return list(self._values)

    def set_chat_names(self, names: dict) -> None:
        """群名表更新后重建标签显示。"""
        self._chat_names = names or {}
        self._rebuild_name_index()
        self.set_values(self._values)

    def _rebuild_name_index(self) -> None:
        """建立 群名 → [关联 ID 列表] 的反查表。"""
        self._name_to_ids = {}
        for cid, name in self._chat_names.items():
            if not name:
                continue
            self._name_to_ids.setdefault(name, []).append(str(cid))

    def _display_id(self, group_id: str) -> str:
        """返回标签上展示的 ID：优先用同名的群号，找不到则用原值。

        消息库用「内部代码」当 chat_id（如 25152178），但用户只认得「群号」
        （如 1109789086）。两者在 chat_names 里映射到同一个群名，所以反查
        同名的另一个 ID 当作展示用的群号。
        """
        name = self._chat_names.get(str(group_id))
        if not name:
            return str(group_id)
        ids = self._name_to_ids.get(name, [])
        for cid in ids:
            if cid != str(group_id):
                return cid
        return str(group_id)

    # ---------- 内部 ----------

    def _make_tag(self, group_id: str) -> QWidget:
        name = self._chat_names.get(str(group_id)) or ""
        display_id = self._display_id(group_id)
        chip = QFrame()
        chip.setFixedHeight(28)
        chip.setStyleSheet(
            f"QFrame {{ background: {theme.CARD_BG.name()};"
            f" border: 1px solid {theme.CARD_BORDER.name()}; border-radius: 14px; }}"
        )
        lay = QHBoxLayout(chip)
        lay.setContentsMargins(10, 0, 4, 0)
        lay.setSpacing(6)

        label = QLabel(f"{name}（{display_id}）" if name else str(group_id))
        label.setStyleSheet(
            f"color: {theme.TEXT.name()}; font-size: 11px; background: transparent;")
        lay.addWidget(label, 1)

        close_btn = QPushButton("×")
        close_btn.setFixedSize(20, 20)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none;"
            f" color: {theme.SUB.name()}; font-size: 14px; border-radius: 10px; }}"
            f"QPushButton:hover {{ background: rgba(255,255,255,15); color: {theme.TEXT.name()}; }}"
        )
        close_btn.clicked.connect(lambda: self._remove(group_id, chip))
        lay.addWidget(close_btn)
        return chip

    def _resolve_id(self, raw: str) -> str:
        """用户输入的可能是群号，要转成消息库用的内部代码才能匹配。

        群号和内部代码在 chat_names 里映射到同一个群名，所以反查同名的
        另一个 ID 就是要转成的目标。如果输入本身就是内部代码则原样返回。
        """
        name = self._chat_names.get(str(raw))
        if not name:
            return str(raw)
        ids = self._name_to_ids.get(name, [])
        for cid in ids:
            if cid != str(raw):
                return cid
        return str(raw)

    def _add_current(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        for chunk in text.replace("，", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            actual = self._resolve_id(chunk)
            if actual not in self._values:
                self._values.append(actual)
                self._flow.addWidget(self._make_tag(actual))
        self._input.clear()

    def _remove(self, group_id: str, chip: QWidget) -> None:
        if group_id in self._values:
            self._values.remove(group_id)
        self._flow.removeWidget(chip)
        chip.deleteLater()


# ---------------------------------------------------------------------------
# 设置面板
# ---------------------------------------------------------------------------

class SettingsPanel(QWidget):
    """四个页签 + 底部保存栏。"""

    config_saved = Signal(dict)
    sync_requested = Signal()
    wipe_requested = Signal()      # 一键清空：由主程序停掉采集线程后执行
    hermes_rotated = Signal()      # Hermes token 已轮换：主程序据此重建服务

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
        self._tabs.addTab(_wrap_scroll(self._build_basic_tab()), "基础")
        self._tabs.addTab(_wrap_scroll(self._build_monitor_tab()), "监听")
        self._tabs.addTab(_wrap_scroll(self._build_ai_tab()), "AI")
        self._tabs.addTab(_wrap_scroll(self._build_archive_tab()), "归档")
        self._tabs.addTab(_wrap_scroll(self._build_link_tab()), "联动")
        self._tabs.addTab(_wrap_scroll(self._build_privacy_tab()), "隐私")
        self._tabs.addTab(_wrap_scroll(self._build_appearance_tab()), "外观")
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
        self._qq_combo = _Combo()
        self._qq_combo.setMinimumWidth(300)
        self._qq_combo.setStyleSheet(_COMBO_STYLE)
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
        lay.addWidget(preview_box)
        lay.addStretch(1)
        return page

    # ---------- 监听 ----------

    def _load_chat_names(self) -> dict:
        """从数据库读 群号→群名 映射，给群标签显示群名用。读失败返回空 dict。"""
        db_path = str(self._cfg.get("db_path") or "")
        if not db_path:
            return {}
        try:
            store = Store(db_path)
            try:
                return store.chat_name_map()
            finally:
                store.close()
        except Exception:  # noqa: BLE001 - 群名只是展示优化，失败不影响功能
            return {}

    def _build_monitor_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)
        uia = self._cfg.get("uia") or {}

        cap_box, cap_form = _group("采集")
        self._uia_enabled = _check(uia.get("enabled", True))
        cap_form.addRow("启用实时监听:", self._uia_enabled)
        self._interval = _spin(int(uia.get("interval") or 2), 1, 600, " 秒")
        cap_form.addRow("抓取间隔:", self._interval)
        self._session_name = _line(str(uia.get("session") or "QQ实时"),
                                   "OCR 通道读不到群名，用这个名字记录来源")
        cap_form.addRow("来源名称:", self._session_name)
        self._ocr_min = _spin(int(round(float(uia.get("ocr_min_confidence") or 0.5) * 100)),
                              0, 100, " %")
        cap_form.addRow("OCR 置信度阈值:", self._ocr_min)
        self._keep_low = _check(uia.get("keep_low_confidence", False),
                                "勾选后低置信度结果仍会入库并标注「OCR低置信度，仅供参考」")
        cap_form.addRow("保留低置信度:", self._keep_low)
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
        chat_names = self._load_chat_names()
        f_box, f_form = _group("免打扰 / 星标")
        self._ignore_groups = GroupTagList(filt.get("ignore_groups"), chat_names)
        f_form.addRow("免打扰:", self._ignore_groups)
        self._star_groups = GroupTagList(filt.get("star_groups"), chat_names)
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
        self._ai_preset = _Combo()
        self._ai_preset.setStyleSheet(_COMBO_STYLE)
        self._ai_preset.addItem("自定义", userData="自定义")
        for name in ("豆包", "ChatGPT", "DeepSeek", "通义千问", "Ollama"):
            self._ai_preset.addItem(name, userData=name)
        self._ai_preset.currentIndexChanged.connect(self._on_preset_changed)
        api_form.addRow("预设模板:", self._ai_preset)

        self._ai_profile = _Combo()
        self._ai_profile.setStyleSheet(_COMBO_STYLE)
        api_form.addRow("配置档案:", self._ai_profile)

        row = QHBoxLayout()
        self._profile_new_btn = QPushButton("新建档案")
        self._profile_new_btn.clicked.connect(self._on_profile_new)
        self._profile_del_btn = QPushButton("删除档案")
        self._profile_del_btn.clicked.connect(self._on_profile_delete)
        row.addWidget(self._profile_new_btn)
        row.addWidget(self._profile_del_btn)
        api_form.addRow("", row)

        self._ai_base = _line(str(ai.get("base_url") or ""),
                              "https://api.openai.com/v1")
        api_form.addRow("Base URL:", self._ai_base)
        self._ai_model = _line(str(ai.get("model") or "gpt-4o-mini"))
        api_form.addRow("模型:", self._ai_model)
        self._ai_key = _line(str(ai.get("api_key") or ""), "也可用环境变量 C2T_AI_KEY")
        self._ai_key.setEchoMode(QLineEdit.EchoMode.Password)
        api_form.addRow("API Key:", self._ai_key)

        self._ai_temp = _FocusDoubleSpinBox()
        self._ai_temp.setRange(0.0, 2.0)
        self._ai_temp.setSingleStep(0.1)
        self._ai_temp.setDecimals(2)
        self._ai_temp.setValue(float(ai.get("temperature") or 0.0))
        self._ai_temp.setStyleSheet(_LINE_STYLE.replace("QLineEdit", "QDoubleSpinBox"))
        api_form.addRow("温度:", self._ai_temp)

        self._ai_top_p = _FocusDoubleSpinBox()
        self._ai_top_p.setRange(0.0, 1.0)
        self._ai_top_p.setSingleStep(0.05)
        self._ai_top_p.setDecimals(2)
        self._ai_top_p.setValue(float(ai.get("top_p") or 1.0))
        self._ai_top_p.setStyleSheet(_LINE_STYLE.replace("QLineEdit", "QDoubleSpinBox"))
        api_form.addRow("Top_p:", self._ai_top_p)

        self._ai_max_tokens = _spin(int(ai.get("max_tokens") or 600), 16, 100000, " tokens")
        api_form.addRow("最大 token:", self._ai_max_tokens)
        self._ai_stream = _check(ai.get("stream", False),
                                 "预留开关：当前版本仍走一次性返回")
        api_form.addRow("流式输出:", self._ai_stream)
        self._ai_timeout = _spin(int(ai.get("timeout") or 15), 3, 120, " 秒")
        api_form.addRow("超时:", self._ai_timeout)

        self._ai_quota = _spin(int(ai.get("tokens_per_day") or 0), 0, 10000000, " tokens/天")
        self._ai_quota.setSpecialValueText("不限")
        api_form.addRow("每日额度:", self._ai_quota)

        test_row = QHBoxLayout()
        self._ai_test_btn = QPushButton("测试连接")
        self._ai_test_btn.clicked.connect(self._on_test_connection)
        self._ai_test_result = QLabel("")
        self._ai_test_result.setStyleSheet(f"color: {theme.SUB.name()}; font-size: 11px;")
        test_row.addWidget(self._ai_test_btn)
        test_row.addWidget(self._ai_test_result, 1)
        api_form.addRow("", test_row)
        api_form.addRow("", _hint(
            "只兼容标准 OpenAI /v1/chat/completions 接口；云端调用前会强制脱敏，"
            "敏感群建议改用本地 Ollama。"))
        lay.addWidget(api_box)
        self._ai_profile.currentIndexChanged.connect(self._on_profile_changed)
        self._refresh_profile_combo(str(ai.get("profile") or "default"))

        usage = ai.get("_usage") if isinstance(ai.get("_usage"), dict) else None
        if usage:
            lay.addWidget(_hint(
                f"本进程已用 {usage.get('tokens', 0)} tokens / "
                f"{usage.get('calls', 0)} 次调用"))
        lay.addStretch(1)
        return page

    # ---------- AI 档案与预设 ----------

    def _profile_names(self) -> list:
        profiles = (self._cfg.get("ai") or {}).get("profiles") or {}
        return sorted(k for k in profiles if isinstance(profiles.get(k), dict))

    def _refresh_profile_combo(self, select: str = "") -> None:
        current = select or self._ai_profile.currentText() or "default"
        self._ai_profile.blockSignals(True)
        self._ai_profile.clear()
        self._ai_profile.addItem("default", userData="default")
        for name in self._profile_names():
            self._ai_profile.addItem(name, userData=name)
        index = self._ai_profile.findData(current)
        self._ai_profile.setCurrentIndex(max(0, index))
        self._ai_profile.blockSignals(False)

    def _apply_profile(self, name: str) -> None:
        """把某套档案的值灌进控件；default 用 ai 段的当前值。"""
        ai = self._cfg.get("ai") or {}
        data = dict(ai) if name == "default" else dict(
            (ai.get("profiles") or {}).get(name) or {})
        if name != "default" and not data:
            return
        self._ai_base.setText(str(data.get("base_url") or ""))
        self._ai_model.setText(str(data.get("model") or "gpt-4o-mini"))
        self._ai_key.setText(str(data.get("api_key") or ""))
        self._ai_temp.setValue(float(data.get("temperature") or 0.0))
        self._ai_top_p.setValue(float(data.get("top_p") or 1.0))
        self._ai_max_tokens.setValue(int(data.get("max_tokens") or 600))
        self._ai_stream.setChecked(bool(data.get("stream", False)))
        self._ai_timeout.setValue(int(data.get("timeout") or 15))
        self._ai_quota.setValue(int(data.get("tokens_per_day") or 0))

    def _profile_payload(self) -> dict:
        return {
            "base_url": self._ai_base.text().strip(),
            "model": self._ai_model.text().strip() or "gpt-4o-mini",
            "api_key": self._ai_key.text().strip(),
            "temperature": round(self._ai_temp.value(), 2),
            "top_p": round(self._ai_top_p.value(), 2),
            "max_tokens": self._ai_max_tokens.value(),
            "stream": self._ai_stream.isChecked(),
            "timeout": self._ai_timeout.value(),
            "tokens_per_day": self._ai_quota.value(),
        }

    def _on_profile_changed(self, _index: int) -> None:
        self._apply_profile(str(self._ai_profile.currentData() or "default"))

    def _on_profile_new(self) -> None:
        name, ok = QInputDialog.getText(self, "新建 AI 档案", "档案名称（如：工作 / 本地）:")
        name = (name or "").strip()
        if not ok or not name or name == "default":
            return
        ai = self._cfg.setdefault("ai", {})
        profiles = ai.setdefault("profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
            ai["profiles"] = profiles
        profiles[name] = self._profile_payload()
        self._refresh_profile_combo(name)
        self._apply_profile(name)

    def _on_profile_delete(self) -> None:
        name = str(self._ai_profile.currentData() or "")
        if not name or name == "default":
            return
        if QMessageBox.question(
                self, "删除档案", f"确定删除档案「{name}」？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        (self._cfg.setdefault("ai", {}).setdefault("profiles", {})).pop(name, None)
        self._refresh_profile_combo("default")
        self._apply_profile("default")

    def _on_preset_changed(self, _index: int) -> None:
        from ..ai import PRESETS

        name = str(self._ai_preset.currentData() or "自定义")
        preset = PRESETS.get(name) or {}
        if preset.get("base_url"):
            self._ai_base.setText(preset["base_url"])
        if preset.get("model"):
            self._ai_model.setText(preset["model"])

    def _on_test_connection(self) -> None:
        if getattr(self, "_probe_worker", None) is not None:
            return
        self._ai_test_btn.setEnabled(False)
        self._ai_test_result.setText("正在测试…")
        self._probe_worker = _ProbeWorker(
            self._ai_base.text().strip(), self._ai_key.text().strip(),
            self._ai_model.text().strip(), self._ai_timeout.value(),
            self._ai_temp.value(), self._ai_top_p.value(), self)
        self._probe_worker.done.connect(self._on_test_done)
        self._probe_worker.finished.connect(self._on_probe_finished)
        self._probe_worker.start()

    def _on_test_done(self, ok: bool, message: str) -> None:
        color = "#22c55e" if ok else "#ef4444"
        self._ai_test_result.setStyleSheet(f"color: {color}; font-size: 11px;")
        self._ai_test_result.setText(message)

    def _on_probe_finished(self) -> None:
        worker = getattr(self, "_probe_worker", None)
        self._probe_worker = None
        if worker is not None:
            worker.deleteLater()
        self._ai_test_btn.setEnabled(True)

    # ---------- 归档 / Obsidian / 备份 ----------

    def _build_archive_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)
        archive = self._cfg.get("archive") or {}
        backup = self._cfg.get("backup") or {}

        box, form = _group("消息归档")
        self._ar_enabled = _check(archive.get("enabled", True),
                                  "关闭后不再写 Markdown 消息归档（数据库不受影响）")
        form.addRow("启用归档:", self._ar_enabled)
        self._ar_dir = _line(str(archive.get("dir") or ""),
                             "留空 = 数据库同目录下的 archive/")
        form.addRow("归档目录:", self._ar_dir)
        self._ar_shot = _check(archive.get("screenshots", False),
                               "截图归档默认关闭；开启后单独目录存储")
        form.addRow("截图归档:", self._ar_shot)
        self._ar_shot_days = _spin(int(archive.get("screenshot_days") or 7), 1, 365, " 天")
        form.addRow("截图保留期:", self._ar_shot_days)
        form.addRow("", _hint("按「群名/年/月/日.md」追加写入，manifest.json 记录已归档的消息 ID"))
        lay.addWidget(box)

        ob_box, ob_form = _group("Obsidian 双向同步（实验特性）")
        self._ob_vault = _line(str(self._cfg.get("obsidian_vault") or ""),
                               "Obsidian 库根目录，例如 D:\\Notes\\MyVault")
        ob_form.addRow("库路径:", self._ob_vault)
        ob_form.addRow("", _hint(
            "只读写 <库>/todoseine/ 下的镜像笔记，不会改动库里其他内容。"
            "默认本地 → Obsidian 单向镜像；在 Obsidian 勾选完成会回写数据库，"
            "删除任务只删镜像。冲突内容另存 .conflict.bak。"))
        lay.addWidget(ob_box)

        bk_box, bk_form = _group("备份")
        self._bk_enabled = _check(backup.get("enabled", True), "每日自动备份一次全量数据库")
        bk_form.addRow("启用备份:", self._bk_enabled)
        self._bk_dir = _line(str(backup.get("dir") or ""),
                             "留空 = 数据库同目录下的 backups/")
        bk_form.addRow("备份目录:", self._bk_dir)
        self._bk_keep = _spin(int(backup.get("keep") or 7), 1, 365, " 份")
        bk_form.addRow("保留份数:", self._bk_keep)
        self._bk_encrypt = _check(backup.get("encrypt", True),
                                  "用 Windows DPAPI 按当前用户加密；换机后无法解密")
        bk_form.addRow("加密备份:", self._bk_encrypt)
        lay.addWidget(bk_box)
        lay.addStretch(1)
        return page

    # ---------- 桌面美化联动 ----------

    def _link_folder(self) -> str:
        """联动输出目录：优先取界面上的值，构建期也能安全调用。"""
        from ..core.desktop_link import out_dir

        cfg = dict(self._cfg)
        desktop = dict(cfg.get("desktop") or {})
        dir_widget = getattr(self, "_dk_dir", None)
        if dir_widget is not None:
            desktop["dir"] = dir_widget.text().strip()
        cfg["desktop"] = desktop
        db_widget = getattr(self, "_db_path", None)
        db_path = db_widget.text().strip() if db_widget is not None else ""
        return out_dir(cfg, db_path or str(cfg.get("db_path") or "") or "qq.db")

    def _link_token(self) -> str:
        """读当前 token；文件不存在时给一句「保存后生成」的说明。"""
        from ..core.desktop_link import load_token

        return load_token(self._link_folder()) or "（保存并重启后自动生成）"

    def _build_link_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)
        desktop = self._cfg.get("desktop") or {}

        box, form = _group("桌面美化联动（DeskBox 等小组件）")
        self._dk_enabled = _check(desktop.get("enabled", False),
                                  "开启后输出脱敏待办文件，并启动仅本机可访问的回写接口")
        form.addRow("启用联动:", self._dk_enabled)
        self._dk_dir = _line(str(desktop.get("dir") or ""),
                             "留空 = 数据库同目录下的 desktop/")
        form.addRow("输出目录:", self._dk_dir)
        self._dk_json = _check(desktop.get("json", True), "输出 todos.json 供小组件读取")
        form.addRow("输出 JSON:", self._dk_json)
        self._dk_ics = _check(desktop.get("ics", True),
                              "输出 todos.ics，可被日历类小组件订阅")
        form.addRow("输出 ICS:", self._dk_ics)
        self._dk_port = _spin(int(desktop.get("port") or 0), 0, 65535)
        form.addRow("本地端口:", self._dk_port)
        form.addRow("", _hint(
            "0 = 随机端口。服务只监听 127.0.0.1，并校验 Host / Origin；"
            "小组件勾选、编辑、删除都会带 token 请求本地接口回写数据库。"
            "输出内容默认脱敏，白名单群与归档口径一致。"))
        lay.addWidget(box)

        tk_box, tk_form = _group("回写令牌")
        token_row = QHBoxLayout()
        self._dk_token = _line("", "")
        self._dk_token.setReadOnly(True)
        self._dk_token.setText(self._link_token())
        token_row.addWidget(self._dk_token, 1)
        copy_btn = QPushButton("复制")
        copy_btn.clicked.connect(self._on_copy_token)
        token_row.addWidget(copy_btn)
        rotate_btn = QPushButton("轮换")
        rotate_btn.setToolTip("立即生成新 token，旧 token 立刻失效")
        rotate_btn.clicked.connect(self._on_rotate_token)
        token_row.addWidget(rotate_btn)
        tk_form.addRow("当前 token:", token_row)
        tk_form.addRow("", _hint(
            "token 写在输出目录的 token.txt（Windows 上用 icacls 只授予当前用户）。"
            "复制给美化工具完成鉴权；怀疑泄露时点「轮换」。"))
        lay.addWidget(tk_box)

        hm_box, hm_form = _group("Hermes 运维服务")
        hermes = self._cfg.get("hermes") or {}
        self._hm_enabled = _check(hermes.get("enabled", True),
                                  "开启后提供本机 token 鉴权的状态查询 / 巡检 / 指令接口")
        hm_form.addRow("启用服务:", self._hm_enabled)
        self._hm_url = _line("", "")
        self._hm_url.setReadOnly(True)
        self._hm_url.setPlaceholderText("服务启动后显示地址")
        hm_form.addRow("服务地址:", self._hm_url)
        hm_token_row = QHBoxLayout()
        self._hm_token = _line("", "")
        self._hm_token.setReadOnly(True)
        self._hm_token.setText(self._hermes_token())
        hm_token_row.addWidget(self._hm_token, 1)
        hm_copy = QPushButton("复制")
        hm_copy.clicked.connect(self._on_hermes_copy)
        hm_token_row.addWidget(hm_copy)
        hm_rotate = QPushButton("轮换")
        hm_rotate.setToolTip("生成新 token；主程序会自动重启服务让旧 token 立即失效")
        hm_rotate.clicked.connect(self._on_hermes_rotate)
        hm_token_row.addWidget(hm_rotate)
        hm_form.addRow("服务 token:", hm_token_row)
        hm_form.addRow("", _hint(
            "token 用 Windows DPAPI 加密保存在 ~/.chat2todo/hermes/token.bin，"
            "只对当前 Windows 用户可解密。服务只监听 127.0.0.1，错误 token 一律 401。"))
        lay.addWidget(hm_box)

        fw_box, fw_form = _group("半自动转发（高风险）")
        self._fw_enabled = _check((self._cfg.get("forward") or {}).get("enabled", False))
        self._fw_enabled.toggled.connect(self._on_forward_toggled)
        fw_form.addRow("启用转发:", self._fw_enabled)
        fw_form.addRow("", _hint(
            "转发会向 QQ 群发送消息，存在发错群 / 发错人的风险，默认关闭。"
            "开启后仅能在看板右键单次触发：先弹预览窗，3 秒倒计时结束才可确认发送，"
            "倒计时期间切换窗口自动取消；每次尝试都写审计日志（目标 / 内容哈希 / 结果）。"))
        lay.addWidget(fw_box)
        lay.addStretch(1)
        return page

    # ---------- Hermes ----------

    def _hermes_token(self) -> str:
        from ..core.hermes import load_token

        return load_token() or "（保存并重启后自动生成）"

    def set_hermes_url(self, url: str) -> None:
        """主程序启动服务后回填地址。"""
        self._hm_url.setText(url)
        if url:
            self._hm_token.setText(self._hermes_token())

    def _on_hermes_copy(self) -> None:
        token = self._hermes_token()
        if token.startswith("（"):
            QMessageBox.information(self, "Hermes token",
                                    "先保存配置并重启程序，token 才会生成。")
            return
        QApplication.clipboard().setText(token)
        QMessageBox.information(self, "Hermes token", "已复制到剪贴板。")

    def _on_hermes_rotate(self) -> None:
        from ..core.hermes import ensure_token

        try:
            token = ensure_token(rotate=True)
        except OSError as exc:
            QMessageBox.critical(self, "Hermes token", f"轮换失败：{exc}")
            return
        self._hm_token.setText(token)
        self.hermes_rotated.emit()
        QMessageBox.information(self, "Hermes token",
                                "新 token 已生成，服务已按新 token 重启；旧 token 立即失效。")

    def _on_forward_toggled(self, checked: bool) -> None:
        if not checked:
            return
        answer = QMessageBox.warning(
            self, "启用半自动转发",
            "转发会向 QQ 群发送消息，可能发错群或发给不该看的人。\n\n"
            "确认要继续吗？开启后每次发送仍会弹预览窗并倒计时 3 秒，需要手动确认。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._fw_enabled.setChecked(False)

    def _on_copy_token(self) -> None:
        token = self._link_token()
        if token.startswith("（"):
            QMessageBox.information(self, "回写令牌", "先保存配置并重启程序，token 才会生成。")
            return
        QApplication.clipboard().setText(token)
        QMessageBox.information(self, "回写令牌", "已复制到剪贴板。")

    def _on_rotate_token(self) -> None:
        from ..core.desktop_link import ensure_token

        try:
            token = ensure_token(self._link_folder(), rotate=True)
        except OSError as exc:
            QMessageBox.critical(self, "回写令牌", f"轮换失败：{exc}")
            return
        self._dk_token.setText(token)
        QMessageBox.information(self, "回写令牌",
                                "新 token 已生成并写入 token.txt，旧 token 立即失效。"
                                "重启程序后本地接口才会使用新 token。")

    # ---------- 隐私 ----------

    def _build_privacy_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)
        privacy = self._cfg.get("privacy") or {}

        base_box, base_form = _group("脱敏")
        self._pr_enabled = _check(privacy.get("enabled", True),
                                  "关闭后手机号 / 身份证等不再打码，仅建议在离线调试时关闭")
        base_form.addRow("启用脱敏:", self._pr_enabled)
        self._pr_logs = _check(privacy.get("mask_logs", True),
                               "日志中的手机号 / 身份证 / 邮箱自动打码")
        base_form.addRow("日志脱敏:", self._pr_logs)
        self._pr_show_original = _check(privacy.get("board_show_original", False),
                                        "关闭时看板列表 / 详情 / 提醒都显示打码后的文本")
        base_form.addRow("看板显示原文:", self._pr_show_original)
        self._pr_raw_ai = _check(privacy.get("allow_raw_ai", False),
                                 "开启后 AI 请求不再打码；默认关闭，开启需要二次确认")
        self._pr_raw_ai.toggled.connect(self._on_raw_ai_toggled)
        base_form.addRow("AI 明文:", self._pr_raw_ai)
        self._pr_raw_groups = _line(_comma_list(privacy.get("raw_ai_groups")),
                                   "群号，逗号分隔；留空 = 开启后所有来源都发明文")
        base_form.addRow("明文群号:", self._pr_raw_groups)
        base_form.addRow("", _hint(
            "数据库仍保存原文（本地不外发）；归档、日志、看板和云端 AI 请求都使用脱敏文本。"
            "云端 AI 请求前一律强制脱敏，白名单群也不例外；只有显式打开「AI 明文」"
            "（可再按群号收窄）才会发送原文。"))
        lay.addWidget(base_box)

        wl_box, wl_form = _group("白名单与自定义规则")
        self._pr_whitelist = _line(_comma_list(privacy.get("whitelist_groups")),
                                   "群名，逗号分隔；这些群在归档 / 看板中不打码")
        wl_form.addRow("白名单群:", self._pr_whitelist)
        self._pr_rules = QPlainTextEdit()
        self._pr_rules.setPlaceholderText(
            "每行一条：名称::正则\n例如：\n工号::\\b[A-Z]{2}\\d{6}\\b")
        self._pr_rules.setPlainText("\n".join(
            str(x) for x in (privacy.get("custom_rules") or [])))
        self._pr_rules.setStyleSheet(
            "QPlainTextEdit { background: rgba(0,0,0,40); color: #cbd5e1;"
            " border: 1px solid rgba(255,255,255,20); border-radius: 6px;"
            " font-family: Consolas, monospace; font-size: 12px; }"
        )
        self._pr_rules.setFixedHeight(110)
        wl_form.addRow("自定义规则:", self._pr_rules)
        wl_form.addRow("", _hint("内置规则：身份证 / 银行卡 / 手机号 / 邮箱；写错的正则会被自动跳过"))
        lay.addWidget(wl_box)
        lay.addStretch(1)
        return page

    def _on_raw_ai_toggled(self, checked: bool) -> None:
        """AI 明文是例外通道：打开必须二次确认，取消就立刻勾回去。"""
        if not checked:
            return
        answer = QMessageBox.warning(
            self, "AI 明文",
            "开启后，待办原文会直接发往云端 AI 接口，可能包含手机号、身份证、"
            "银行卡、邮箱等个人信息，且不再打码。\n\n"
            "建议只在离线本机模型或明确可信的群里使用。确认要开启吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._pr_raw_ai.setChecked(False)

    # ---------- 外观 ----------

    def _build_appearance_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)
        dash = self._cfg.get("dash") or {}

        view_box, view_form = _group("看板")
        self._radius = _spin(int(dash.get("radius") or 14), 0, 32, " px")
        view_form.addRow("窗口圆角:", self._radius)
        self._border = _spin(int(dash.get("border") or 1), 0, 6, " px")
        view_form.addRow("边框宽度:", self._border)
        self._border_color = _line(str(dash.get("border_color") or ""),
                                   "#RRGGBB，留空用主题色")
        view_form.addRow("边框颜色:", self._border_color)
        self._clock_cb = _check(dash.get("clock", True))
        view_form.addRow("显示时钟:", self._clock_cb)
        self._anim_cb = _check(dash.get("anim", True))
        view_form.addRow("交互动画:", self._anim_cb)

        # 动画速度（DeskBox 的 widgetAnimationSpeed）
        self._anim_speed = _Combo()
        self._anim_speed.setFixedHeight(28)
        self._anim_speed.setStyleSheet(_COMBO_STYLE)
        self._anim_speed.addItem("关闭（瞬间完成）", "off")
        self._anim_speed.addItem("快", "fast")
        self._anim_speed.addItem("标准", "normal")
        self._anim_speed.addItem("慢", "slow")
        _speed = str(dash.get("anim_speed") or "normal").lower()
        _si = self._anim_speed.findData(_speed)
        self._anim_speed.setCurrentIndex(_si if _si >= 0 else 2)
        view_form.addRow("动画速度:", self._anim_speed)

        # 动画效果（DeskBox 的 widgetAnimationEffect）
        self._anim_effect = _Combo()
        self._anim_effect.setFixedHeight(28)
        self._anim_effect.setStyleSheet(_COMBO_STYLE)
        self._anim_effect.addItem("滑动", "slide")
        self._anim_effect.addItem("淡入淡出", "fade")
        self._anim_effect.addItem("无动效", "none")
        _eff = str(dash.get("anim_effect") or "slide").lower()
        _ei = self._anim_effect.findData(_eff)
        self._anim_effect.setCurrentIndex(_ei if _ei >= 0 else 0)
        view_form.addRow("动画效果:", self._anim_effect)

        # 系统圆角（DeskBox 的 widgetCornerPreference）
        self._corner_round = _check(dash.get("corner_round", True),
                                    "关闭后窗口四角改为直角（跟随 DWM 圆角偏好）")
        view_form.addRow("系统圆角:", self._corner_round)

        # 边框颜色模式（DeskBox 的 widgetBorderColorMode）
        self._border_accent = _check(dash.get("border_accent", False),
                                     "关闭=中性灰边框，开启=跟随强调色")
        view_form.addRow("边框跟随强调色:", self._border_accent)

        # 系统材质下拉：六种选项，值写进 dash.material（str）
        # 兼容旧配置：material=True 视作 "acrylic"，False 视作 "off"
        self._material_combo = _Combo()
        self._material_combo.setFixedHeight(28)
        self._material_combo.setStyleSheet(_COMBO_STYLE)
        self._material_combo.addItem("关闭（纯色深色）", "off")
        self._material_combo.addItem("亚克力 · Acrylic", "acrylic")
        self._material_combo.addItem("Mica", "mica")
        self._material_combo.addItem("Mica Alt", "mica_alt")
        self._material_combo.addItem("Tabbed", "tabbed")
        self._material_combo.addItem("毛玻璃 · Blur", "blur")
        raw_mat = dash.get("material", True)
        mat_val = _material_from_raw(raw_mat)
        idx = self._material_combo.findData(mat_val)
        self._material_combo.setCurrentIndex(idx if idx >= 0 else 1)
        view_form.addRow("系统材质:", self._material_combo)

        # 材质浓淡（tint alpha）：只在亚克力下生效
        self._material_intensity = _spin(
            int(round(float(dash.get("material_intensity") or 0.65) * 100)),
            10, 100, " %")
        view_form.addRow("材质浓淡:", self._material_intensity)

        self._snap_cb = _check(dash.get("snap", True))
        view_form.addRow("边缘吸附:", self._snap_cb)
        self._snap_gap = _spin(int(dash.get("snap_gap") or 16), 2, 60, " px")
        view_form.addRow("吸附距离:", self._snap_gap)
        self._lock_cb = _check(dash.get("lock", False))
        view_form.addRow("锁定位置:", self._lock_cb)
        self._footer_stats_cb = _check(dash.get("footer_stats", True),
                                       "页脚显示「待办 x · 已完成 y · 本周 z」")
        view_form.addRow("页脚统计:", self._footer_stats_cb)
        self._clear_done_cb = _check(dash.get("clear_done_button", True),
                                    "页脚右下角的「清除已完成」按钮")
        view_form.addRow("清除已完成按钮:", self._clear_done_cb)

        # 输入框回车行为（DeskBox 的 todoEditorEnterBehavior）
        self._todo_enter = _Combo()
        self._todo_enter.setFixedHeight(28)
        self._todo_enter.setStyleSheet(_COMBO_STYLE)
        self._todo_enter.addItem("回车直接添加", "enter")
        self._todo_enter.addItem("Ctrl+回车才添加", "ctrl_enter")
        _te = str(dash.get("todo_enter") or "enter").lower()
        _ti = self._todo_enter.findData(_te)
        self._todo_enter.setCurrentIndex(_ti if _ti >= 0 else 0)
        view_form.addRow("回车行为:", self._todo_enter)
        self._opacity = _spin(int(round(float(dash.get("opacity") or 0.94) * 100)),
                              50, 100, " %")
        view_form.addRow("不透明度:", self._opacity)
        self._no_activate_cb = _check(dash.get("no_activate", True),
                                     "点击 / 拖拽 / 勾选不抢占前台窗口焦点；点输入框时临时恢复")
        view_form.addRow("不抢焦点:", self._no_activate_cb)
        self._auto_hide_cb = _check(dash.get("auto_hide", True),
                                   "贴左 / 右边缘时，鼠标离开自动收起只留一条边")
        view_form.addRow("贴边自动收起:", self._auto_hide_cb)
        self._exclude_capture_cb = _check(
            dash.get("exclude_from_capture", False),
            "截屏 / 录屏 / 远程桌面时隐藏本窗口（含敏感待办时建议开启）")
        view_form.addRow("截屏隐藏:", self._exclude_capture_cb)
        view_form.addRow("", _hint("位置与尺寸会自动记住；圆角 / 边框 / 不透明度 / 材质 / 动画速度保存后即时生效"))
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
        reset_btn = QPushButton("恢复默认")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        reset_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.CARD_BG.name()};"
            f" color: {theme.SUB.name()}; border: 1px solid {theme.CARD_BORDER.name()};"
            " border-radius: 7px; font-size: 11px; padding: 3px 12px; }"
            f"QPushButton:hover {{ background: {theme.CARD_HOVER.name()};"
            f" color: {theme.TEXT.name()}; }}"
            f"QPushButton:pressed {{ background: {theme.CARD_PRESSED.name()}; }}"
        )
        reset_btn.clicked.connect(self._reset_thresholds)
        thr_form.addRow("", reset_btn)
        lay.addWidget(thr_box)

        store_box, store_form = _group("数据与日志")
        self._db_path = _line(str(self._cfg.get("db_path") or ""))
        store_form.addRow("数据库路径:", self._db_path)

        # QQ 库直连密钥：DPAPI 加密存 ~/todoseine/dbkey.dat，只有当前用户本机可解
        # 走 UIA 监控 QQ 窗口的用户不需要这个；直连 QQ 加密数据库时才需要
        dbkey_row = QHBoxLayout()
        self._db_key_edit = QLineEdit()
        self._db_key_edit.setPlaceholderText("留空表示不修改")
        self._db_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._db_key_edit.setStyleSheet(_LINE_STYLE)
        dbkey_row.addWidget(self._db_key_edit, 1)
        save_key_btn = QPushButton("保存")
        save_key_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_key_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.ACCENT.name()};"
            f" color: {theme.ACCENT_TEXT.name()}; border: none; border-radius: 6px;"
            " padding: 6px 14px; font-size: 12px; }"
            f"QPushButton:hover {{ background: "
            f"{theme.mix(theme.ACCENT, theme.PANEL, 0.15).name()}; }}"
        )
        save_key_btn.clicked.connect(self._on_save_db_key)
        dbkey_row.addWidget(save_key_btn)
        store_form.addRow("QQ 库密钥:", dbkey_row)
        self._db_key_status = _hint("")
        self._refresh_db_key_status()
        store_form.addRow("", self._db_key_status)
        store_form.addRow("", _hint(
            "用途：直连 QQ 加密数据库读取消息。走 QQ 窗口截图识别（默认）的用户不需要配置。\n"
            "获取：此密钥是 QQ 数据库的加密口令，需从 QQ 安装目录或相关工具中取得。\n"
            "安全：用 Windows DPAPI 加密保存，仅本机当前 Windows 用户可解密，不会明文落盘。\n"
            "清除：输入框留空后点「保存」即可删除已保存的密钥。"))

        self._log_level = _Combo()
        for level in ("debug", "info", "warning", "error"):
            self._log_level.addItem(level, level)
        idx = self._log_level.findData(str(self._cfg.get("log_level") or "info"))
        if idx >= 0:
            self._log_level.setCurrentIndex(idx)
        self._log_level.setStyleSheet(_COMBO_STYLE)
        store_form.addRow("日志级别:", self._log_level)

        wipe_btn = QPushButton("一键清空程序数据…")
        wipe_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        wipe_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: #ef4444;"
            f" border: 1px solid {theme.CARD_BORDER.name()}; border-radius: 6px;"
            " padding: 6px 14px; font-size: 12px; }"
            f"QPushButton:hover {{ border-color: #ef4444; }}"
        )
        wipe_btn.clicked.connect(self._on_wipe_clicked)
        store_form.addRow("危险操作:", wipe_btn)
        store_form.addRow("", _hint(
            "清空范围：数据库、消息归档、备份、回收站、日志、缓存、插件配置；"
            "不会删除插件本体，也不会动 Obsidian 库里的其他笔记。"))
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
            self._qq_status.setText("QQ 未启动：请先打开 QQ 并登录，再点「刷新」")
            return
        from ..sources.uia import qq_window_info

        saved = int((self._cfg.get("uia") or {}).get("hwnd") or 0)
        checks: list = []
        for i, (hwnd, title, pid) in enumerate(wins):
            info = qq_window_info(hwnd)
            version = info.get("version") or "版本未知"
            self._qq_combo.addItem(
                f"PID {pid} — {title} · QQ {version} (hwnd={hwnd})", userData=hwnd)
            checks.append(version)
            if hwnd == saved:
                self._qq_combo.setCurrentIndex(i)
        if saved == 0:
            self._qq_combo.setCurrentIndex(0)
        self._qq_hwnd = int(self._qq_combo.currentData() or 0)
        self._qq_combo.currentIndexChanged.connect(self._on_qq_changed)
        self._qq_status.setText(
            f"已选择 hwnd={self._qq_hwnd} · {len(wins)} 个窗口 · "
            f"QQ {checks[0] if checks else '版本未知'}")

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

    def _reset_thresholds(self) -> None:
        """判定阈值恢复默认：待办 4，待确认 3。"""
        self._todo_thr.setValue(4)
        self._pending_thr.setValue(3)

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
            "ocr_min_confidence": self._ocr_min.value() / 100.0,
            "keep_low_confidence": self._keep_low.isChecked(),
        }
        cfg["notify"] = {
            "new_todo": self._notify_new.isChecked(),
            "due": self._notify_due.isChecked(),
            "due_within": self._due_within.value(),
        }
        cfg["filter"] = {
            "ignore_groups": self._ignore_groups.values(),
            "star_groups": self._star_groups.values(),
            "star_senders": list((self._cfg.get("filter") or {}).get("star_senders") or []),
        }
        profile_name = str(self._ai_profile.currentData() or "default")
        profiles = dict((self._cfg.get("ai") or {}).get("profiles") or {})
        if profile_name != "default":
            profiles[profile_name] = self._profile_payload()   # 当前编辑内容写回该档案
        payload = self._profile_payload()
        cfg["ai"] = {
            "enabled": self._ai_enabled.isChecked(),
            "review": self._ai_review.isChecked(),
            "merge": self._ai_merge.isChecked(),
            "intent": self._ai_intent.isChecked(),
            "rewrite": self._ai_rewrite.isChecked(),
            "base_url": payload["base_url"],
            "model": payload["model"],
            "api_key": payload["api_key"],
            "temperature": payload["temperature"],
            "top_p": payload["top_p"],
            "max_tokens": payload["max_tokens"],
            "stream": payload["stream"],
            "timeout": payload["timeout"],
            "tokens_per_day": payload["tokens_per_day"],
            "profile": profile_name,
            "profiles": profiles,
        }
        cfg["archive"] = {
            "enabled": self._ar_enabled.isChecked(),
            "dir": self._ar_dir.text().strip(),
            "screenshots": self._ar_shot.isChecked(),
            "screenshot_days": self._ar_shot_days.value(),
        }
        cfg["backup"] = {
            "enabled": self._bk_enabled.isChecked(),
            "dir": self._bk_dir.text().strip(),
            "keep": self._bk_keep.value(),
            "encrypt": self._bk_encrypt.isChecked(),
        }
        cfg["obsidian_vault"] = self._ob_vault.text().strip()
        cfg["desktop"] = {
            "enabled": self._dk_enabled.isChecked(),
            "dir": self._dk_dir.text().strip(),
            "json": self._dk_json.isChecked(),
            "ics": self._dk_ics.isChecked(),
            "port": self._dk_port.value(),
            "debounce_ms": int((self._cfg.get("desktop") or {}).get("debounce_ms") or 1500),
        }
        cfg["hermes"] = {
            "enabled": self._hm_enabled.isChecked(),
            "port": int((self._cfg.get("hermes") or {}).get("port") or 0),
            "auto_restart": bool((self._cfg.get("hermes") or {}).get("auto_restart", True)),
            "daily_summary": bool((self._cfg.get("hermes") or {}).get("daily_summary", True)),
            "summary_time": (self._cfg.get("hermes") or {}).get("summary_time", "21:30"),
        }
        cfg["forward"] = {"enabled": self._fw_enabled.isChecked()}
        cfg["privacy"] = {
            "enabled": self._pr_enabled.isChecked(),
            "mask_logs": self._pr_logs.isChecked(),
            "board_show_original": self._pr_show_original.isChecked(),
            "whitelist_groups": _parse_list(self._pr_whitelist.text()),
            "allow_raw_ai": self._pr_raw_ai.isChecked(),
            "raw_ai_groups": _parse_list(self._pr_raw_groups.text()),
            "custom_rules": [
                ln.strip() for ln in self._pr_rules.toPlainText().splitlines()
                if "::" in ln
            ],
        }
        dash = dict(self._cfg.get("dash") or {})
        dash.update({
            "radius": self._radius.value(),
            "border": self._border.value(),
            "border_color": self._border_color.text().strip(),
            "clock": self._clock_cb.isChecked(),
            "anim": self._anim_cb.isChecked(),
            "anim_speed": self._anim_speed.currentData(),
            "anim_effect": self._anim_effect.currentData(),
            "corner_round": self._corner_round.isChecked(),
            "border_accent": self._border_accent.isChecked(),
            "material": self._material_combo.currentData(),
            "material_intensity": self._material_intensity.value() / 100.0,
            "snap": self._snap_cb.isChecked(),
            "snap_gap": self._snap_gap.value(),
            "footer_stats": self._footer_stats_cb.isChecked(),
            "clear_done_button": self._clear_done_cb.isChecked(),
            "todo_enter": self._todo_enter.currentData(),
            "lock": self._lock_cb.isChecked(),
            "opacity": self._opacity.value() / 100.0,
            "no_activate": self._no_activate_cb.isChecked(),
            "auto_hide": self._auto_hide_cb.isChecked(),
            "exclude_from_capture": self._exclude_capture_cb.isChecked(),
        })
        cfg["dash"] = dash
        cfg["hotkey"] = {
            "enabled": self._hk_enabled.isChecked(),
            "show": self._hk_show.text().strip() or "ctrl+alt+d",
            "snap": self._hk_snap.text().strip() or "ctrl+shift+s",
        }
        # 同步旧布局，tk 控制台/旧配置仍能读到同一套组合
        cfg["dash"] = {**(cfg.get("dash") or {}), "hotkey": cfg["hotkey"]["show"]}
        cfg["snap"] = {**(cfg.get("snap") or {}), "hotkey": cfg["hotkey"]["snap"]}
        return cfg

    def _save_config(self) -> None:
        cfg = self._collect()
        if not cfg.get("db_path"):
            QMessageBox.warning(self, "配置", "数据库路径不能为空。")
            return
        try:
            c2t_config.save(cfg, cfg.get("config_file") or None)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "配置", f"保存失败：{exc}")
            return
        self._cfg = cfg
        self._status.setText("已保存 · 立即生效")
        QTimer.singleShot(2500, lambda: self._status.setText(""))
        self.config_saved.emit(dict(cfg))

    def _on_wipe_clicked(self) -> None:
        """两道确认：先讲清范围，再要求明确同意不可恢复。"""
        first = QMessageBox.question(
            self, "一键清空",
            "将删除所有程序生成的数据：数据库、消息归档、备份、回收站、日志、缓存、插件配置。\n\n"
            "不会删除插件本体，也不会修改 Obsidian 库中的其他笔记。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if first != QMessageBox.StandardButton.Yes:
            return
        second = QMessageBox.warning(
            self, "确认清空",
            "此操作不可恢复，清空后程序将退出。确定继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if second != QMessageBox.StandardButton.Yes:
            return
        self.wipe_requested.emit()

    # ---------- QQ 库密钥 ----------

    def _refresh_db_key_status(self) -> None:
        """刷新密钥状态：是否已保存、存在哪里。"""
        if not hasattr(self, "_db_key_status"):
            return
        if keybox.has_key():
            self._db_key_status.setText(
                f"已保存（{keybox.key_file_path()}，DPAPI 加密，仅本机当前用户可解）")
        else:
            self._db_key_status.setText("未保存；直连 QQ 加密数据库时才需要，走 UIA 监控可忽略")

    def _on_save_db_key(self) -> None:
        """保存 QQ 库直连密钥：空输入=清除，非空=DPAPI 加密保存。"""
        text = self._db_key_edit.text()
        if not text.strip():
            if keybox.has_key():
                keybox.delete_key()
                self._db_key_status.setText("已清除密钥")
            else:
                self._db_key_status.setText("输入框为空，未做任何更改")
            self._db_key_edit.clear()
            return
        try:
            path = keybox.save_key(text.strip())
        except Exception as exc:  # noqa: BLE001 - 保存失败给用户提示
            QMessageBox.warning(self, "保存密钥失败", str(exc))
            return
        self._db_key_edit.clear()
        self._db_key_status.setText(f"已保存到 {path}（DPAPI 加密）")

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
        self._ai_temp.setValue(float(ai.get("temperature") or 0.0))
        self._ai_top_p.setValue(float(ai.get("top_p") or 1.0))
        self._ai_max_tokens.setValue(int(ai.get("max_tokens") or 600))
        self._ai_stream.setChecked(bool(ai.get("stream", False)))
        self._ai_timeout.setValue(int(ai.get("timeout") or 15))
        self._ai_quota.setValue(int(ai.get("tokens_per_day") or 0))
        self._refresh_profile_combo(str(ai.get("profile") or "default"))

        archive = cfg.get("archive") or {}
        backup = cfg.get("backup") or {}
        self._ar_enabled.setChecked(bool(archive.get("enabled", True)))
        self._ar_dir.setText(str(archive.get("dir") or ""))
        self._ar_shot.setChecked(bool(archive.get("screenshots", False)))
        self._ar_shot_days.setValue(int(archive.get("screenshot_days") or 7))
        self._bk_enabled.setChecked(bool(backup.get("enabled", True)))
        self._bk_dir.setText(str(backup.get("dir") or ""))
        self._bk_keep.setValue(int(backup.get("keep") or 7))
        self._bk_encrypt.setChecked(bool(backup.get("encrypt", True)))
        self._ob_vault.setText(str(cfg.get("obsidian_vault") or ""))

        desktop = cfg.get("desktop") or {}
        self._dk_enabled.setChecked(bool(desktop.get("enabled", False)))
        self._dk_dir.setText(str(desktop.get("dir") or ""))
        self._dk_json.setChecked(bool(desktop.get("json", True)))
        self._dk_ics.setChecked(bool(desktop.get("ics", True)))
        self._dk_port.setValue(int(desktop.get("port") or 0))
        self._dk_token.setText(self._link_token())

        hermes = cfg.get("hermes") or {}
        self._hm_enabled.setChecked(bool(hermes.get("enabled", True)))
        self._hm_token.setText(self._hermes_token())
        self._fw_enabled.blockSignals(True)   # 载入配置时不该弹二次确认
        self._fw_enabled.setChecked(bool((cfg.get("forward") or {}).get("enabled", False)))
        self._fw_enabled.blockSignals(False)

        privacy = cfg.get("privacy") or {}
        self._pr_enabled.setChecked(bool(privacy.get("enabled", True)))
        self._pr_logs.setChecked(bool(privacy.get("mask_logs", True)))
        self._pr_show_original.setChecked(bool(privacy.get("board_show_original", False)))
        self._pr_whitelist.setText(_comma_list(privacy.get("whitelist_groups")))
        self._pr_raw_ai.blockSignals(True)   # 载入配置时不该弹二次确认
        self._pr_raw_ai.setChecked(bool(privacy.get("allow_raw_ai", False)))
        self._pr_raw_ai.blockSignals(False)
        self._pr_raw_groups.setText(_comma_list(privacy.get("raw_ai_groups")))
        self._pr_rules.setPlainText("\n".join(
            str(x) for x in (privacy.get("custom_rules") or [])))

        self._uia_enabled.setChecked(bool(uia.get("enabled", True)))
        self._interval.setValue(int(uia.get("interval") or 2))
        self._ocr_min.setValue(int(round(float(uia.get("ocr_min_confidence") or 0.5) * 100)))
        self._keep_low.setChecked(bool(uia.get("keep_low_confidence", False)))
        self._session_name.setText(str(uia.get("session") or "QQ实时"))

        self._notify_new.setChecked(bool(notify.get("new_todo", True)))
        self._notify_due.setChecked(bool(notify.get("due", True)))
        self._due_within.setValue(int(notify.get("due_within") or 3600))
        chat_names = self._load_chat_names()
        self._ignore_groups.set_chat_names(chat_names)
        self._star_groups.set_chat_names(chat_names)
        self._ignore_groups.set_values(filt.get("ignore_groups"))
        self._star_groups.set_values(filt.get("star_groups"))

        self._radius.setValue(int(dash.get("radius") or 14))
        self._border.setValue(int(dash.get("border") or 1))
        self._border_color.setText(str(dash.get("border_color") or ""))
        self._clock_cb.setChecked(bool(dash.get("clock", True)))
        self._anim_cb.setChecked(bool(dash.get("anim", True)))
        _si = self._anim_speed.findData(
            str(dash.get("anim_speed") or "normal").lower())
        self._anim_speed.setCurrentIndex(_si if _si >= 0 else 2)
        _ei = self._anim_effect.findData(
            str(dash.get("anim_effect") or "slide").lower())
        self._anim_effect.setCurrentIndex(_ei if _ei >= 0 else 0)
        self._corner_round.setChecked(bool(dash.get("corner_round", True)))
        self._border_accent.setChecked(bool(dash.get("border_accent", False)))
        raw_mat = dash.get("material", True)
        idx = self._material_combo.findData(_material_from_raw(raw_mat))
        self._material_combo.setCurrentIndex(idx if idx >= 0 else 1)
        self._material_intensity.setValue(
            int(round(float(dash.get("material_intensity") or 0.65) * 100)))
        self._snap_cb.setChecked(bool(dash.get("snap", True)))
        self._snap_gap.setValue(int(dash.get("snap_gap") or 16))
        self._footer_stats_cb.setChecked(bool(dash.get("footer_stats", True)))
        self._clear_done_cb.setChecked(bool(dash.get("clear_done_button", True)))
        _ti = self._todo_enter.findData(
            str(dash.get("todo_enter") or "enter").lower())
        self._todo_enter.setCurrentIndex(_ti if _ti >= 0 else 0)
        self._lock_cb.setChecked(bool(dash.get("lock")))
        self._opacity.setValue(int(round(float(dash.get("opacity") or 0.94) * 100)))
        self._no_activate_cb.setChecked(bool(dash.get("no_activate", True)))
        self._auto_hide_cb.setChecked(bool(dash.get("auto_hide", True)))
        self._exclude_capture_cb.setChecked(
            bool(dash.get("exclude_from_capture", False)))
        self._hk_enabled.setChecked(bool(hk.get("enabled", True)))
        self._hk_show.setText(str(hk.get("show") or "ctrl+alt+d"))
        self._hk_snap.setText(str(hk.get("snap") or "ctrl+shift+s"))

        self._todo_thr.setValue(int(cfg.get("todo_threshold", 4)))
        self._pending_thr.setValue(int(cfg.get("pending_threshold", 3)))
        self._db_path.setText(str(cfg.get("db_path") or ""))
        self._refresh_db_key_status()
        idx = self._log_level.findData(str(cfg.get("log_level") or "info"))
        if idx >= 0:
            self._log_level.setCurrentIndex(idx)
        self._refresh_qq_list()
        self._status.setText("已重新载入")
        QTimer.singleShot(2000, lambda: self._status.setText(""))
