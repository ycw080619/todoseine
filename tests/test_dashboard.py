"""看板数据层测试（不启动 GUI，可在无显示环境跑）。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from chat2todo.adapters import get_adapter
from chat2todo.core import Store, dedupe, extract_todos
from chat2todo.dashboard import (_anchor_of, _apply_anchor, _clamp_to_rects,
                                 _ease_out_cubic, _extract_filenames,
                                 _slide_offset, _snap_to_edges, due_remindable,
                                 filter_items, load_items)
from chat2todo.models import TodoItem

SAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples",
    "sample_qq_export.txt",
)


class TestDashboardData(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        messages = dedupe(list(get_adapter("qq_txt").read(SAMPLE)))
        todos = extract_todos(messages, session="demo")
        store = Store(self.db)
        try:
            store.add_messages(messages)
            store.add_todos(todos)
        finally:
            store.close()

    def tearDown(self) -> None:
        try:
            os.remove(self.db)
        except OSError:
            pass

    def test_load_pending(self) -> None:
        items = load_items(self.db, "pending")
        self.assertEqual(len(items), 3)
        self.assertTrue(all(item.status == "pending" for item in items))
        self.assertTrue(all(item.todo_id for item in items))

    def test_toggle_to_done_and_reload(self) -> None:
        items = load_items(self.db, "pending")
        store = Store(self.db)
        try:
            self.assertTrue(store.set_status(items[0].todo_id, "done"))
        finally:
            store.close()
        # 当天勾掉的条目仍留在待办列表里（划线沉底），不会立刻消失
        pending = load_items(self.db, "pending")
        self.assertEqual(len(pending), 3)
        self.assertEqual(pending[-1].status, "done")      # 沉到最底
        self.assertEqual(len(load_items(self.db, "done")), 1)

        # 第二天开机归档：进回收站，不再出现在待办里
        store = Store(self.db)
        try:
            store.conn.execute("UPDATE todos SET done_at='2000-01-01' WHERE status='done'")
            store.conn.commit()
            self.assertEqual(store.archive_completed(), 1)
            self.assertEqual(store.count_trash(), 1)
        finally:
            store.close()
        self.assertEqual(len(load_items(self.db, "pending")), 2)

    def test_add_todo_from_dashboard(self) -> None:
        """看板「＋ 添加」走的就是这个入口，必须能落库并出现在待办里。"""
        store = Store(self.db)
        try:
            self.assertEqual(store.add_todo("手动加一条测试待办"), 1)
        finally:
            store.close()
        texts = [item.text for item in load_items(self.db, "pending")]
        self.assertIn("手动加一条测试待办", texts)
        self.assertEqual(len(texts), 4)

    def test_unconfirmed_items_show_on_dashboard(self) -> None:
        """3 分判为「待确认」的条目不能被看板吞掉。"""
        store = Store(self.db)
        try:
            store.add_todo("半信半疑的一条")          # 手动添加是 pending
            store.conn.execute("UPDATE todos SET status='unconfirmed' WHERE text='半信半疑的一条'")
            store.conn.commit()
        finally:
            store.close()
        texts = [item.text for item in load_items(self.db, "pending")]
        self.assertIn("半信半疑的一条", texts)
        self.assertEqual(len(texts), 4)
        self.assertEqual(len(load_items(self.db, "unconfirmed")), 1)

    def test_update_text(self) -> None:
        """看板行尾「改」走 update_text：改完能读回新内容，其它字段不动。"""
        store = Store(self.db)
        try:
            store.add_todo("改之前的文字")
            tid = [t for t in store.list_todos()
                   if t.text == "改之前的文字"][0].todo_id
            self.assertTrue(store.update_text(tid, "改之后的文字"))
        finally:
            store.close()
        items = load_items(self.db, "pending")
        texts = [item.text for item in items]
        self.assertIn("改之后的文字", texts)
        self.assertNotIn("改之前的文字", texts)


class TestMonitorClamp(unittest.TestCase):
    """多显示器钳位：副屏拔掉后坐标不能落在虚空里（纯函数，不碰 Win32）。"""

    RECTS = [(0, 0, 1920, 1040), (1920, 0, 3840, 1080)]   # 主屏（含任务栏）+ 右副屏

    def test_keeps_visible_position(self) -> None:
        self.assertEqual(_clamp_to_rects(100, 100, 360, 540, self.RECTS),
                         (100, 100))

    def test_keeps_second_monitor_position(self) -> None:
        self.assertEqual(_clamp_to_rects(3000, 200, 360, 540, self.RECTS),
                         (3000, 200))

    def test_pulls_back_when_monitor_unplugged(self) -> None:
        # 副屏（x≥1920）没了：坐标 2500 落在虚空 → 贴回主屏右边缘
        self.assertEqual(_clamp_to_rects(2500, 300, 360, 540,
                                         [(0, 0, 1920, 1040)]),
                         (1920 - 48, 300))

    def test_pulls_back_from_left_void(self) -> None:
        # 完全在左屏之外：拉回后至少露出 margin=48px
        x, y = _clamp_to_rects(-500, 100, 360, 540, [(0, 0, 1920, 1040)])
        self.assertEqual((x, y), (-360 + 48, 100))

    def test_picks_nearest_monitor(self) -> None:
        # 两台之间偏副屏的虚空点（上方）→ 钳进副屏：横向已够可见不动，纵向拉回
        x, y = _clamp_to_rects(1880, -600, 360, 540, self.RECTS)
        self.assertEqual((x, y), (1880, -540 + 48))

    def test_empty_rects_returns_as_is(self) -> None:
        self.assertEqual(_clamp_to_rects(9999, 9999, 360, 540, []),
                         (9999, 9999))

    def test_margin_is_respected(self) -> None:
        # 露出不足 margin 也算不可见，会被钳回去
        x, y = _clamp_to_rects(1900, 100, 360, 540, [(0, 0, 1920, 1040)])
        self.assertEqual(x, 1920 - 48)


class TestEasing(unittest.TestCase):
    def test_ease_out_cubic(self) -> None:
        self.assertEqual(_ease_out_cubic(0), 0)
        self.assertEqual(_ease_out_cubic(1), 1)
        self.assertGreater(_ease_out_cubic(0.5), 0.5)   # 起步快、收尾慢
        self.assertEqual(_ease_out_cubic(2), 1)          # 越界钳位
        self.assertEqual(_ease_out_cubic(-1), 0)


class TestSnapToEdges(unittest.TestCase):
    """边缘吸附：松手时距工作区边缘 <=spacing 就贴上去（纯函数）。"""

    RECTS = [(0, 0, 1920, 1040), (1920, 0, 3840, 1080)]   # 主屏 + 右副屏

    def test_snaps_to_left_edge(self) -> None:
        self.assertEqual(_snap_to_edges(5, 300, 360, 540, self.RECTS),
                         (0, 300))

    def test_snaps_to_right_edge(self) -> None:
        # 窗口右缘 1555+360=1915，距主屏右缘 5px → 吸到 1920-360
        self.assertEqual(_snap_to_edges(1555, 300, 360, 540, self.RECTS),
                         (1560, 300))

    def test_snaps_to_top_and_bottom(self) -> None:
        self.assertEqual(_snap_to_edges(400, 4, 360, 540, self.RECTS),
                         (400, 0))
        # 下缘 495+540=1035，距工作区底 5px → 吸到 1040-540
        self.assertEqual(_snap_to_edges(400, 495, 360, 540, self.RECTS),
                         (400, 500))

    def test_beyond_threshold_stays(self) -> None:
        self.assertEqual(_snap_to_edges(20, 300, 360, 540, self.RECTS),
                         (20, 300))

    def test_second_monitor_edges(self) -> None:
        # 中心点在副屏：吸副屏左缘（1920）而不是主屏右缘
        self.assertEqual(_snap_to_edges(1925, 300, 360, 540, self.RECTS),
                         (1920, 300))

    def test_spacing_zero_disables(self) -> None:
        self.assertEqual(_snap_to_edges(5, 5, 360, 540, self.RECTS, 0),
                         (5, 5))

    def test_empty_rects_returns_as_is(self) -> None:
        self.assertEqual(_snap_to_edges(5, 5, 360, 540, []), (5, 5))


class TestAnchor(unittest.TestCase):
    """四角锚点：分辨率不变时原样还原，变了就贴住原来的角落。"""

    RECT = (0, 0, 1920, 1040)

    def test_roundtrip_right_bottom(self) -> None:
        a = _anchor_of(1560, 500, 360, 540, self.RECT)
        self.assertEqual(a, ("rb", 0, 0))
        self.assertEqual(_apply_anchor(a, 360, 540, self.RECT), (1560, 500))

    def test_roundtrip_left_top(self) -> None:
        a = _anchor_of(20, 30, 360, 540, self.RECT)
        self.assertEqual(a, ("lt", 20, 30))
        self.assertEqual(_apply_anchor(a, 360, 540, self.RECT), (20, 30))

    def test_follows_corner_after_resolution_change(self) -> None:
        # 右下角贴边的窗口，换到 1600x900 的工作区后仍贴右下角
        a = _anchor_of(1560, 500, 360, 540, self.RECT)
        self.assertEqual(_apply_anchor(a, 360, 540, (0, 0, 1600, 900)),
                         (1240, 360))


class TestSlideOffset(unittest.TestCase):
    """唤出滑入的起点：朝最近的工作区边缘偏移。"""

    RECTS = [(0, 0, 1920, 1040)]

    def test_slides_from_right(self) -> None:
        # 靠右（中心距右缘 180，是最近的边）→ 从右边 24px 外滑进来
        self.assertEqual(_slide_offset(1560, 300, 360, 540, self.RECTS),
                         (1584, 300))

    def test_slides_from_left(self) -> None:
        self.assertEqual(_slide_offset(8, 300, 360, 540, self.RECTS),
                         (-16, 300))

    def test_slides_from_top(self) -> None:
        self.assertEqual(_slide_offset(400, 4, 360, 540, self.RECTS),
                         (400, -20))

    def test_empty_rects_returns_as_is(self) -> None:
        self.assertEqual(_slide_offset(5, 5, 360, 540, []), (5, 5))


class TestExtractFilenames(unittest.TestCase):
    """从待办文字 / 原文消息里识别 QQ 下载文件的文件名。"""

    def test_qq_file_prefix(self) -> None:
        self.assertEqual(_extract_filenames(["[文件] 周报.docx"]), ["周报.docx"])

    def test_filename_in_sentence(self) -> None:
        self.assertEqual(_extract_filenames(["把 报价单v2.xlsx 周五前发我"]),
                         ["报价单v2.xlsx"])

    def test_no_filename(self) -> None:
        self.assertEqual(_extract_filenames(["记得吃饭", "好的"]), [])

    def test_dedupe_and_order(self) -> None:
        self.assertEqual(
            _extract_filenames(["收 数据.csv", "再发一次 数据.csv 和 图.png"]),
            ["数据.csv", "图.png"])


class TestDueRemindable(unittest.TestCase):
    """到期提醒的挑选逻辑：快到期 / 刚过期的未完成条目，按到期升序。"""

    NOW = datetime(2026, 10, 27, 12, 0, 0)

    def _item(self, text, status="pending", tid=1):
        return TodoItem(text=text, session="s", sender="u",
                        message_ts=0, status=status, todo_id=tid)

    def test_picks_due_soon_and_overdue_sorted(self) -> None:
        items = [self._item("今天20点前交报告", tid=1),      # 8 小时后到期
                 self._item("10月26日交报告", tid=2),        # 已过期约 12 小时
                 self._item("没有期限的待办", tid=3)]
        pairs = due_remindable(items, self.NOW, within=9 * 3600)
        self.assertEqual([p[0].todo_id for p in pairs], [2, 1])   # 过期的排最前

    def test_skips_done_and_far_future(self) -> None:
        items = [self._item("今天12点半前交报告", status="done", tid=1),
                 self._item("下个月5号再说", tid=2)]
        self.assertEqual(due_remindable(items, self.NOW), [])

    def test_within_window(self) -> None:
        items = [self._item("今天14点前交报告", tid=1)]     # 2 小时后
        self.assertEqual(due_remindable(items, self.NOW, within=3600), [])
        self.assertEqual(len(due_remindable(items, self.NOW, within=3 * 3600)), 1)


class TestFilterItems(unittest.TestCase):
    """看板搜索框的过滤逻辑：大小写不敏感子串，匹配内容 / 发送人。"""

    def _item(self, text, sender="张三", tid=1):
        return TodoItem(text=text, session="s", sender=sender,
                        message_ts=0, status="pending", todo_id=tid)

    def test_empty_query_returns_all(self) -> None:
        items = [self._item("写周报", tid=1), self._item("买牛奶", tid=2)]
        self.assertEqual(len(filter_items(items, "")), 2)
        self.assertEqual(len(filter_items(items, "   ")), 2)

    def test_matches_text_case_insensitive(self) -> None:
        items = [self._item("把 ReportFinal.docx 发群里", tid=1),
                 self._item("写周报", tid=2)]
        hit = filter_items(items, "reportfinal")
        self.assertEqual([i.todo_id for i in hit], [1])

    def test_matches_sender(self) -> None:
        items = [self._item("写周报", sender="李四", tid=1),
                 self._item("买牛奶", sender="张三", tid=2)]
        hit = filter_items(items, "李四")
        self.assertEqual([i.todo_id for i in hit], [1])

    def test_no_match_returns_empty(self) -> None:
        items = [self._item("写周报", tid=1)]
        self.assertEqual(filter_items(items, "不存在的关键词"), [])


class _FakeMenu:
    """记录菜单调用序列的最小替身：不需要 Tk，也不需要显示器。"""

    def __init__(self) -> None:
        self.calls = []

    def delete(self, *args) -> None:
        self.calls.append(("delete", args))

    def add_command(self, label: str, command) -> None:
        self.calls.append(("command", label, command))

    def add_separator(self) -> None:
        self.calls.append(("separator",))

    def labels(self) -> list:
        return [c[1] for c in self.calls if c[0] == "command"]


class TestItemMenu(unittest.TestCase):
    """右键菜单填充逻辑：曾因 menu_ref 未定义而每次右键必崩，这里守住菜单内容。"""

    def setUp(self) -> None:
        self.marked = []

        def mark(item, status):
            self.marked.append((item.todo_id, status))

        self.mark = mark
        self.touched = []

    def _fill(self, status: str) -> _FakeMenu:
        from chat2todo.dashboard.app import _fill_item_menu

        item = TodoItem(text="写周报", session="群A", sender="张三",
                        message_ts=0, todo_id=7, status=status)
        menu = _FakeMenu()
        _fill_item_menu(menu, item, mark=self.mark,
                        edit_item=lambda i: self.touched.append(("edit", i.todo_id)),
                        show_detail=lambda i: self.touched.append(("detail", i.todo_id)),
                        explain_why=lambda i: self.touched.append(("why", i.todo_id)))
        return menu

    def test_pending_menu(self) -> None:
        menu = self._fill("pending")
        self.assertEqual(menu.calls[0], ("delete", (0, "end")))
        self.assertEqual(menu.labels(), ["标记完成", "标为非待办", "修改内容",
                                         "查看原文", "判定理由"])
        menu.calls[1][2]()                      # 触发「标记完成」
        self.assertEqual(self.marked, [(7, "done")])

    def test_done_menu_offers_restore(self) -> None:
        menu = self._fill("done")
        self.assertIn("恢复为待办", menu.labels())
        self.assertNotIn("标记完成", menu.labels())

    def test_actions_route_to_handlers(self) -> None:
        menu = self._fill("pending")
        for call in menu.calls:
            if call[0] == "command" and call[1] in ("修改内容", "查看原文", "判定理由"):
                call[2]()
        self.assertEqual(self.touched, [("edit", 7), ("detail", 7), ("why", 7)])

    def test_mute_menu_items(self) -> None:
        """看板右键要能选「此类消息不再提醒」，且保留按人 / 按群。"""
        from chat2todo.dashboard.app import _fill_item_menu

        muted = []
        item = TodoItem(text="通知", session="群A", sender="张三",
                        message_ts=0, todo_id=7, status="pending")
        menu = _FakeMenu()
        _fill_item_menu(menu, item, mark=self.mark,
                        edit_item=lambda i: None, show_detail=lambda i: None,
                        explain_why=lambda i: None,
                        mute=lambda i, scope: muted.append((i.todo_id, scope)))
        labels = menu.labels()
        self.assertIn("此类消息不再提醒", labels)
        self.assertIn("此人不再提醒", labels)
        self.assertIn("整个来源不再提醒", labels)
        for call in menu.calls:
            if call[0] == "command" and "不再提醒" in call[1]:
                call[2]()
        self.assertEqual(muted, [(7, "kind"), (7, "sender"), (7, "source")])


class TestMaterialConfig(unittest.TestCase):
    """[ui] material 配置：off/acrylic/mica 三态，非法值回退 off。

    材质模式的两条铁律（都来自实测，改回去 Acrylic 就会失效）：
      1. 不能用 -transparentcolor 颜色键；
      2. 不能用 -alpha（会挂 WS_EX_LAYERED），所以 alpha 强制 1.0。
    """

    def setUp(self) -> None:
        from chat2todo.dashboard import app
        self.app = app
        self._old_home = os.environ.get("USERPROFILE")

    def tearDown(self) -> None:
        if self._old_home is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = self._old_home
        self.app.MATERIAL = "off"

    def _apply(self, toml_text: str) -> dict:
        import pathlib

        d = tempfile.mkdtemp()
        p = pathlib.Path(d) / "Chat2Todo"
        p.mkdir()
        (p / "c2t.toml").write_text(toml_text, encoding="utf-8")
        os.environ["USERPROFILE"] = d
        return self.app._apply_ui_config()

    def test_default_is_off(self) -> None:
        ui = self._apply("[ui]\n")
        self.assertEqual(self.app.MATERIAL, "off")
        self.assertEqual(ui["alpha"], 0.93)     # 默认透明度不受材质影响

    def test_acrylic_forces_opaque(self) -> None:
        ui = self._apply('[ui]\nmaterial = "acrylic"\nopacity = 0.5\n')
        self.assertEqual(self.app.MATERIAL, "acrylic")
        self.assertEqual(ui["alpha"], 1.0)      # 材质模式禁用整窗半透明

    def test_mica_accepted(self) -> None:
        self._apply('[ui]\nmaterial = "mica"\n')
        self.assertEqual(self.app.MATERIAL, "mica")

    def test_invalid_value_falls_back_to_off(self) -> None:
        self._apply('[ui]\nmaterial = "glass"\n')
        self.assertEqual(self.app.MATERIAL, "off")

    def test_win_alpha_noop_in_material_mode(self) -> None:
        calls = []

        class _FakeRoot:
            def attributes(self, *a):
                calls.append(a)

        root = _FakeRoot()
        self.app.MATERIAL = "acrylic"
        self.app._win_alpha(root, 0.5)
        self.assertEqual(calls, [])             # 材质模式：绝不能挂 layered
        self.app.MATERIAL = "off"
        self.app._win_alpha(root, 0.5)
        self.assertEqual(calls, [("-alpha", 0.5)])


if __name__ == "__main__":
    unittest.main()
