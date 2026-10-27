"""看板数据层测试（不启动 GUI，可在无显示环境跑）。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from chat2todo.adapters import get_adapter
from chat2todo.core import Store, dedupe, extract_todos
from chat2todo.dashboard import _clamp_to_rects, _ease_out_cubic, load_items

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


if __name__ == "__main__":
    unittest.main()
