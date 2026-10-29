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
    from chat2todo import config

    path = tmp_path / "config.toml"
    db = tmp_path / "test.db"
    config.save({"db_path": str(db), "todo_threshold": 4,
                 "pending_threshold": 3, "log_level": "info"}, str(path))
    monkeypatch.setenv("C2T_CONFIG", str(path))
    return str(path)


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_hotkey_parse_basic():
    from chat2todo.qt.hotkey import parse

    mods, vk = parse("ctrl+alt+d")
    assert vk == ord("D")
    assert mods & 0x0002 and mods & 0x0001      # Ctrl + Alt
    assert parse("") is None
    assert parse("ctrl+") is None
    assert parse("ctrl+shift+nosuchkey") is None


def test_hotkey_parse_letters_and_specials():
    from chat2todo.qt.hotkey import parse

    assert parse("ctrl+shift+s") is not None
    assert parse("win+f5") is not None


def test_build_tray_icon(qapp):
    from chat2todo.qt.tray_icon import build_icon

    icon = build_icon()
    assert not icon.isNull()
    assert icon.pixmap(32).width() == 32


def test_extract_batch_empty():
    from chat2todo.qt.monitor import extract_batch

    todos, decisions = extract_batch([], {})
    assert todos == []
    assert decisions == []


def test_extract_batch_picks_todo():
    from chat2todo.models import Message
    from chat2todo.qt.monitor import extract_batch

    msgs = [Message(timestamp=1_700_000_000, sender_id="1", sender_name="老王",
                    content="明天下午三点前把周报发我一下，谢谢")]
    todos, _decisions = extract_batch(msgs, {"todo_threshold": 4,
                                            "pending_threshold": 3})
    assert len(todos) == 1
    assert todos[0].sender == "老王"


def test_settings_parse_list():
    from chat2todo.qt.settings_panel import _parse_list

    assert _parse_list("123, 456，789\n123") == ["123", "456", "789"]
    assert _parse_list("") == []


# ---------------------------------------------------------------------------
# 窗口与页面
# ---------------------------------------------------------------------------

def test_dashboard_offscreen(qapp, cfg_file, tmp_path):
    from chat2todo.qt.dashboard import Dashboard

    view = Dashboard()
    try:
        view.reload(force=True)
        view.notify_new_todos([])       # 没有新待办时不该弹
        assert view._db_path.endswith("test.db")
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_main_window_pages(qapp, cfg_file):
    from chat2todo.qt.main_window import FramelessMainWindow

    win = FramelessMainWindow()
    try:
        assert win._stack.count() == 6
        win._switch_page(1)
        win._switch_page(2)
        win._switch_page(4)

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


def test_pages_render_without_db(qapp, cfg_file):
    from chat2todo.qt.pages import AboutPage, StatsPage, TrashPage

    for page in (TrashPage(""), StatsPage(""), AboutPage("")):
        try:
            if hasattr(page, "refresh"):
                page.refresh()
        finally:
            page.deleteLater()
    qapp.processEvents()


def test_trash_roundtrip(qapp, cfg_file):
    from chat2todo.core import Store
    from chat2todo.qt.pages import TrashPage

    from chat2todo import config

    db = config.load()["db_path"]
    store = Store(db)
    todo_id = store.add_todo("回收站测试", session="测试", sender="我")
    assert store.trash_item(todo_id)
    assert store.count_trash() == 1
    store.close()

    page = TrashPage(db)
    try:
        row_id = Store(db).list_trash(1)[0]["id"]
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


def test_normalize_cjk_spaces():
    from chat2todo.sources.uia import normalize_cjk_spaces

    assert normalize_cjk_spaces("记 得 明 天 交 周 报") == "记得明天交周报"
    # 中英/数字之间的空格要保留
    assert normalize_cjk_spaces("明天 10:30 开会") == "明天 10:30 开会"
    # 全角标点两侧的空格要压掉（OCR 出来的时间戳常带空格）
    assert normalize_cjk_spaces("19 ： 04 开会") == "19：04 开会"
    assert normalize_cjk_spaces("（ 16 ）可以 ，我主要做调查 。") == "（16）可以，我主要做调查。"


def test_capture_chat_without_window():
    """非 Windows 或句柄无效时安全返回 None，不抛异常。"""
    from chat2todo.sources.uia import _capture_chat

    assert _capture_chat(0) is None


def test_snap_dialog_ingest(qapp, cfg_file, tmp_path):
    """截图入库：不走 OCR，直接喂文本，验证抽取 + 写库。"""
    from chat2todo import config
    from chat2todo.core import Store
    from chat2todo.qt.snap import SnapController

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
