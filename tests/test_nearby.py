"""看板「点文字看原文」所用查询的测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from chat2todo.core import Store  # noqa: E402
from chat2todo.models import Message  # noqa: E402


class TestNearbyMessages(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        store = Store(self.db)
        try:
            store.add_messages([
                Message(timestamp=1000, sender_id="1", sender_name="小李", content="第一条"),
                Message(timestamp=1060, sender_id="2", sender_name="小张", content="第二条"),
                Message(timestamp=5000, sender_id="1", sender_name="小李", content="远处的一条"),
            ])
        finally:
            store.close()

    def tearDown(self) -> None:
        try:
            os.remove(self.db)
        except OSError:
            pass

    def test_returns_nearest_messages(self) -> None:
        store = Store(self.db)
        try:
            rows = store.nearby_messages(1060, 2)
        finally:
            store.close()
        self.assertEqual(len(rows), 2)
        # 最近的应当是时间戳 1060 与 1000 这两条
        contents = [r["content"] for r in rows]
        self.assertIn("第二条", contents)
        self.assertIn("第一条", contents)
        self.assertNotIn("远处的一条", contents)

    def test_limit_respected(self) -> None:
        store = Store(self.db)
        try:
            self.assertEqual(len(store.nearby_messages(1000, 1)), 1)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
