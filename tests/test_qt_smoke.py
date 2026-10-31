"""Qt 层冒烟测试：只要不崩、关键纯函数行为正确即可。

全部在 offscreen 平台跑（CI / 无桌面会话也能过），不依赖真实 QQ 与 OCR。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def cfg_file(tmp_path, monkeypatch):
    """把配置与数据库都指向临时目录，避免污染用户真实数据。"""
    from todoseine import config

    path = tmp_path / "config.toml"
    db = tmp_path / "test.db"
    config.save({"db_path": str(db), "todo_threshold": 4,
                 "pending_threshold": 3, "log_level": "info"}, str(path))
    monkeypatch.setenv("C2T_CONFIG", str(path))
    return str(path)


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_launcher_brief_is_short_text():
    """诊断信息必须是一段短文本，且任何平台都不许抛异常。"""
    from todoseine.qt.tray import launcher_brief

    text = launcher_brief()
    assert isinstance(text, str)
    assert text
    assert len(text) <= 120


def test_hotkey_parse_basic():
    from todoseine.qt.hotkey import parse

    mods, vk = parse("ctrl+alt+d")
    assert vk == ord("D")
    assert mods & 0x0002 and mods & 0x0001      # Ctrl + Alt
    assert parse("") is None
    assert parse("ctrl+") is None
    assert parse("ctrl+shift+nosuchkey") is None


def test_hotkey_parse_letters_and_specials():
    from todoseine.qt.hotkey import parse

    assert parse("ctrl+shift+s") is not None
    assert parse("win+f5") is not None


def test_build_tray_icon(qapp):
    from todoseine.qt.tray_icon import build_icon

    icon = build_icon()
    assert not icon.isNull()
    assert icon.pixmap(32).width() == 32


def test_extract_batch_empty():
    from todoseine.qt.monitor import extract_batch

    todos, decisions = extract_batch([], {})
    assert todos == []
    assert decisions == []


def test_extract_batch_picks_todo():
    from todoseine.models import Message
    from todoseine.qt.monitor import extract_batch

    msgs = [Message(timestamp=1_700_000_000, sender_id="1", sender_name="老王",
                    content="明天下午三点前把周报发我一下，谢谢")]
    todos, _decisions = extract_batch(msgs, {"todo_threshold": 4,
                                            "pending_threshold": 3})
    assert len(todos) == 1
    assert todos[0].sender == "老王"


def test_settings_parse_list():
    from todoseine.qt.settings_panel import _parse_list

    assert _parse_list("123, 456，789\n123") == ["123", "456", "789"]
    assert _parse_list("") == []


# ---------------------------------------------------------------------------
# 窗口与页面
# ---------------------------------------------------------------------------

def test_dashboard_offscreen(qapp, cfg_file, tmp_path):
    from todoseine.qt.dashboard import Dashboard

    view = Dashboard()
    try:
        view.reload(force=True)
        view.notify_new_todos([])       # 没有新待办时不该弹
        assert view._db_path.endswith("test.db")
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_main_window_pages(qapp, cfg_file):
    from todoseine.qt.main_window import FramelessMainWindow

    win = FramelessMainWindow()
    try:
        # 悬浮窗口只承载看板 + 回收站 + mini panel = 3 页；
        # 设置/统计/关于已拆到独立窗口
        assert win._stack.count() == 3
        win._switch_page(0)
        win._switch_page(1)

        geo = win.geometry()
        win.toggle_mini()
        assert win.is_mini()
        assert win.width() <= 320
        win.toggle_mini()
        assert not win.is_mini()
        assert win.geometry().width() == geo.width()
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_main_window_resize_eight_directions(qapp, cfg_file):
    """四边 + 四角拖拽改尺寸：命中判定与几何计算。"""
    from PySide6.QtCore import QPoint, QRect

    from todoseine.qt.main_window import FramelessMainWindow

    win = FramelessMainWindow()
    try:
        win.setGeometry(QRect(100, 100, 600, 450))
        w, h = win.width(), win.height()

        hits = {
            QPoint(2, 2): "top-left",
            QPoint(w - 2, 2): "top-right",
            QPoint(2, h - 2): "bottom-left",
            QPoint(w - 2, h - 2): "bottom-right",
            QPoint(2, h // 2): "left",
            QPoint(w - 2, h // 2): "right",
            QPoint(w // 2, 2): "top",
            QPoint(w // 2, h - 2): "bottom",
            QPoint(w // 2, h // 2): "",
        }
        for pos, expect in hits.items():
            assert win._edge_at(pos) == expect, (pos, expect)

        # 锁定后不再命中可拖拽区域
        win._dash_cfg["lock"] = True
        assert win._edge_at(QPoint(2, h // 2)) == ""
        win._dash_cfg["lock"] = False

        base = win.geometry()

        def drag(edge, dx, dy):
            win._resize_edge = edge
            win._resize_start = (QPoint(0, 0), base)
            win._do_resize(QPoint(dx, dy))

        drag("right", 20, 0)
        assert win.width() == base.width() + 20 and win.x() == base.x()

        win.setGeometry(base)
        drag("left", -20, 0)
        assert win.width() == base.width() + 20 and win.x() == base.x() - 20

        win.setGeometry(base)
        drag("bottom", 0, 20)
        assert win.height() == base.height() + 20 and win.y() == base.y()

        win.setGeometry(base)
        drag("top", 0, -20)
        assert win.height() == base.height() + 20 and win.y() == base.y() - 20

        win.setGeometry(base)
        drag("bottom-right", 20, 20)
        assert (win.width() == base.width() + 20
                and win.height() == base.height() + 20)

        # 下限：宽不小于 460，高不小于 420
        win.setGeometry(base)
        drag("right", -9999, 0)
        assert win.width() >= 460
        win.setGeometry(base)
        drag("bottom", 0, -9999)
        assert win.height() >= 420
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_pages_render_without_db(qapp, cfg_file):
    from todoseine.qt.pages import AboutPage, StatsPage, TrashPage

    for page in (TrashPage(""), StatsPage(""), AboutPage("")):
        try:
            if hasattr(page, "refresh"):
                page.refresh()
        finally:
            page.deleteLater()
    qapp.processEvents()


def test_trash_roundtrip(qapp, cfg_file):
    from todoseine.core import Store
    from todoseine.qt.pages import TrashPage

    from todoseine import config

    db = config.load()["db_path"]
    store = Store(db)
    todo_id = store.add_todo("回收站测试", session="测试", sender="我")
    trash_id = store.trash_item(todo_id)
    # 返回回收站记录 id（撤销要用），不再是裸 bool
    assert isinstance(trash_id, int) and trash_id > 0
    assert store.count_trash() == 1
    store.close()

    page = TrashPage(db)
    try:
        row_id = Store(db).list_trash(1)[0]["id"]
        assert row_id == trash_id
        page._restore(row_id)
        check = Store(db)
        try:
            assert check.count_trash() == 0
            texts = [t.text for t in check.list_todos()]
            assert "回收站测试" in texts
        finally:
            check.close()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_restore_keeps_done_status(cfg_file):
    """已完成的任务删进回收站，还原后不该变回待办再提醒一遍。"""
    from todoseine import config
    from todoseine.core import Store

    db = config.load()["db_path"]
    store = Store(db)
    todo_id = store.add_todo("已完成再还原", session="测试", sender="我")
    store.set_status(todo_id, "done")
    trash_id = store.trash_item(todo_id)
    assert store.restore(trash_id)
    todo = store.conn.execute(
        "SELECT status FROM todos WHERE text='已完成再还原'").fetchone()
    assert todo["status"] == "done"
    store.close()


def test_trash_missing_todo_returns_none(cfg_file):
    from todoseine import config
    from todoseine.core import Store

    store = Store(config.load()["db_path"])
    assert store.trash_item(999_999) is None
    store.close()


def test_mini_parser_roundtrip(tmp_path):
    """save() 写出的 TOML 必须能被 3.9/3.10 的极简解析器原样读回。

    之前引号里的反斜杠不回解，Windows 路径读出来是双反斜杠，库直接打不开。
    """
    from todoseine import config

    cfg = {
        "db_path": r"C:\Users\someone\Chat2Todo\qq.db",
        "todo_threshold": 4,
        "vector": {"sim_threshold": 0.92, "enabled": True},
        "filter": {"ignore_groups": ["群, 带逗号", "123456"]},
        "config_file": str(tmp_path / "c.toml"),
    }
    path = config.save(cfg, str(tmp_path / "c.toml"))
    back = config._mini_parse_text(open(path, encoding="utf-8").read())
    assert back["db_path"] == r"C:\Users\someone\Chat2Todo\qq.db"
    assert back["vector"]["sim_threshold"] == 0.92
    assert back["filter"]["ignore_groups"] == ["群, 带逗号", "123456"]


def test_monitor_stop_is_clean(cfg_file, qapp, monkeypatch):
    """重复 start 不能起第二个线程；stop 后引用必须清掉，不留正在跑的 QThread。

    stop 超时还把引用置空会让线程在运行中被析构，Qt 直接 abort，
    所以这里盯着 _worker 的归属和状态。
    """
    import todoseine.qt.monitor as mon

    monkeypatch.setattr(mon, "fetch_messages", lambda **kwargs: [])

    from todoseine import config

    monitor = mon.Monitor(cfg=config.load())
    monitor.start()
    first = monitor._worker
    monitor.start()                    # 重复按「启用监视」
    assert monitor._worker is first
    assert first is not None and first.isRunning()
    monitor.stop()
    assert monitor._worker is None
    assert monitor.status() == "已停止"


def test_crop_drops_sidebar_and_member_panel():
    """左会话列表 + 聊天区 + 右侧群成员面板，裁完只剩聊天区。

    群成员面板展开时，如果拿「最右一列」当聊天区底色，左边分界会认错，
    成员名单会被当成消息 OCR 进库。这里用合成图钉住三条边界。
    """
    from PIL import Image, ImageDraw

    from todoseine.sources import uia

    img = Image.new("RGB", (1000, 400), (44, 44, 44))       # 聊天区底色
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 259, 399], fill=(56, 56, 56))        # 会话列表
    d.rectangle([780, 0, 999, 399], fill=(36, 36, 36))      # 群成员面板
    for y in range(80, 340, 40):                            # 几条消息气泡
        d.rectangle([300, y, 520, y + 24], fill=(64, 64, 64))

    assert 775 <= uia._detect_chat_right(0, img) <= 790
    assert 250 <= uia._detect_chat_left(0, img) <= 275

    cropped = uia._cut_sidebar(0, img)
    assert 400 < cropped.width < 560        # 列表与面板都被切掉，只剩聊天区
    assert cropped.width == 780 - 250 or abs(cropped.width - 526) < 40


def test_crop_keeps_full_width_without_panel():
    """没开面板时不能把右边也裁掉。"""
    from PIL import Image, ImageDraw

    from todoseine.sources import uia

    img = Image.new("RGB", (900, 300), (44, 44, 44))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 239, 299], fill=(56, 56, 56))
    assert uia._detect_chat_right(0, img) == 900


def test_locate_worker_runs_off_main_thread(qapp, monkeypatch):
    """定位下载文件走子线程：run() 只调扫描函数并把结果发回来。"""
    import todoseine.qt.dashboard as dash

    monkeypatch.setattr(dash, "_find_downloaded_file",
                        lambda names: rf"C:\Downloads\{names[0]}")
    got: list = []
    worker = dash._LocateWorker(["报告.zip"])
    worker.found.connect(got.append)
    try:
        worker.run()
        assert got == [r"C:\Downloads\报告.zip"]
    finally:
        worker.deleteLater()


def test_normalize_cjk_spaces():
    from todoseine.sources.uia import normalize_cjk_spaces

    assert normalize_cjk_spaces("记 得 明 天 交 周 报") == "记得明天交周报"
    # 中英/数字之间的空格要保留
    assert normalize_cjk_spaces("明天 10:30 开会") == "明天 10:30 开会"
    # 全角标点两侧的空格要压掉（OCR 出来的时间戳常带空格）
    assert normalize_cjk_spaces("19 ： 04 开会") == "19：04 开会"
    assert normalize_cjk_spaces("（ 16 ）可以 ，我主要做调查 。") == "（16）可以，我主要做调查。"


def test_capture_chat_without_window():
    """非 Windows 或句柄无效时安全返回 None，不抛异常。"""
    from todoseine.sources.uia import _capture_chat

    assert _capture_chat(0) is None


def test_snap_dialog_ingest(qapp, cfg_file, tmp_path):
    """截图入库：不走 OCR，直接喂文本，验证抽取 + 写库。"""
    from todoseine import config
    from todoseine.core import Store
    from todoseine.qt.snap import SnapController

    db = config.load()["db_path"]
    ctl = SnapController(lambda: db)
    saved: list = []
    ctl.saved.connect(lambda total, added: saved.append((total, added)))
    ctl._ingest("明天前把合同签好发我\n今天记得订会议室")
    assert saved, "截图入库没有回报结果"
    total, added = saved[0]
    assert total >= 1 and added >= 1

    store = Store(db)
    try:
        assert store.list_todos()
    finally:
        store.close()


def test_smooth_scroll_step_is_pixels_not_singlestep(qapp):
    """滚轮一格要滚几十像素，不是 QScrollBar.singleStep() 的默认 1px。

    DeskBox 借鉴版误用 singleStep 当步长，滚一格只动 1px，体感就是滚不动。
    这里钉住：一格(120)的目标位移 ≈72px。
    """
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QWidget

    from todoseine.qt.dashboard import SmoothScrollArea

    scroll = SmoothScrollArea()
    try:
        body = QWidget()
        body.setMinimumHeight(2000)
        scroll.setWidget(body)
        scroll.resize(200, 200)
        scroll.show()
        qapp.processEvents()

        event = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, 0),
                            QPoint(0, -120), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        scroll.wheelEvent(event)
        # 动画异步执行，钉目标值即可；≈72px，绝不能退化成 singleStep 的 1px
        assert 50 <= scroll._target <= 100
        scroll._anim.stop()

        # anim 关掉后退回原生瞬时滚动，不崩即可
        scroll.anim_enabled = False
        scroll.wheelEvent(event)
    finally:
        scroll.deleteLater()
        qapp.processEvents()


def test_todo_row_children_transparent_for_hover(qapp):
    """文字/勾选点必须透传鼠标事件，否则 hover 进出判定被子控件截胡。

    鼠标扫过标题文字时父卡片会收 leaveEvent，hover 底色与边框高亮瞬间消退，
    体感就是「没有叠加效果」。按钮（改/⋯）不能透明——它们要自己收点击。
    """
    from PySide6.QtCore import Qt

    from todoseine.models import TodoItem
    from todoseine.qt.dashboard import TodoRow
    from todoseine.qt.widgets.text import ElidedLabel

    row = TodoRow(TodoItem(text="明天交周报", session="测试", sender="老王",
                           message_ts=1_700_000_000), 0)
    try:
        transparent = Qt.WidgetAttribute.WA_TransparentForMouseEvents
        assert row.dot.testAttribute(transparent)
        labels = row.findChildren(ElidedLabel)
        assert len(labels) == 2
        assert all(lb.testAttribute(transparent) for lb in labels)
        # 按钮保持可点击
        assert not row.edit_btn.testAttribute(transparent)
        assert not row.more_btn.testAttribute(transparent)
    finally:
        row.deleteLater()
        qapp.processEvents()
