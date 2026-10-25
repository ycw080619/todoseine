"""增量复核：只挑没复核过的条目，复核后打标记。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from chat2todo.core import Store  # noqa: E402


class TestIncrementalReview(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self) -> None:
        try:
            os.remove(self.db)
        except OSError:
            pass

    def test_new_todos_start_unreviewed(self) -> None:
        store = Store(self.db)
        try:
            store.add_todo("明天前把周报发我")
            items = store.unreviewed_todos()
            self.assertEqual(len(items), 1)
        finally:
            store.close()

    def test_mark_reviewed_removes_from_queue(self) -> None:
        store = Store(self.db)
        try:
            store.add_todo("交实验报告")
            item = store.unreviewed_todos()[0]
            self.assertTrue(store.mark_reviewed(item.todo_id, "pending"))
            self.assertEqual(len(store.unreviewed_todos()), 0)
            # 状态同时被更新
            pend = store.list_todos("pending")
            self.assertEqual(len(pend), 1)
        finally:
            store.close()

    def test_mark_reviewed_keeps_status_when_omitted(self) -> None:
        store = Store(self.db)
        try:
            store.add_todo("随便一条")
            item = store.unreviewed_todos()[0]
            before = item.status
            store.mark_reviewed(item.todo_id)
            after = store.list_todos(None)[0].status
            self.assertEqual(before, after)
        finally:
            store.close()

    def test_old_rows_migrated_as_reviewed(self) -> None:
        """模拟老库：没有 reviewed 列时，升级后历史条目不重跑。"""
        import sqlite3

        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            CREATE TABLE todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
                session TEXT NOT NULL DEFAULT '', sender TEXT NOT NULL DEFAULT '',
                ts INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT '', UNIQUE (text, ts, sender));
            INSERT INTO todos (text, ts, status) VALUES ('历史条目', 1700000000, 'pending');
            """
        )
        conn.commit()
        conn.close()

        store = Store(self.db)  # 触发迁移
        try:
            self.assertEqual(len(store.unreviewed_todos()), 0)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
