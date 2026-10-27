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
                                 load_items)
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


if __name__ == "__main__":
    unittest.main()
