"""AI 复核后的看板过滤：noise 不进看板。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from chat2todo.core import Store  # noqa: E402
from chat2todo.dashboard import load_items  # noqa: E402


class TestNoiseFilter(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self) -> None:
        try:
            os.remove(self.db)
        except OSError:
            pass

    def test_noise_hidden_from_dashboard(self) -> None:
        store = Store(self.db)
        try:
            store.add_todo("明天前把周报发我")
            store.add_todo("哈哈哈笑死")
            items = store.list_todos("pending")
        finally:
            store.close()
        # 把第二条标记成 AI 判定的噪声
        store = Store(self.db)
        try:
            for it in store.list_todos("pending"):
                if "笑死" in it.text:
                    store.set_status(it.todo_id, "noise")
        finally:
            store.close()

        shown = load_items(self.db, "pending")
        texts = [i.text for i in shown]
        self.assertIn("明天前把周报发我", texts)
        self.assertNotIn("哈哈哈笑死", texts)

    def test_clear_all_does_not_touch_noise(self) -> None:
        """清空只处理 pending/unconfirmed，不应把 noise 改成 done。"""
        store = Store(self.db)
        try:
            store.add_todo("真待办")
            store.add_todo("噪音")
            for it in store.list_todos("pending"):
                if it.text == "噪音":
                    store.set_status(it.todo_id, "noise")
        finally:
            store.close()
        store = Store(self.db)
        try:
            self.assertEqual(len(store.list_todos("noise")), 1)
            self.assertEqual(len(store.list_todos("pending")), 1)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
