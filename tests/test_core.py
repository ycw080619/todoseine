"""核心逻辑单元测试。运行：python -m unittest discover -s tests"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from chat2todo.adapters import get_adapter  # noqa: E402
from chat2todo.core import Store, dedupe, extract_todos, render_markdown  # noqa: E402
from chat2todo.models import Message, MsgType  # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "sample_qq_export.txt")


class TestQQAdapter(unittest.TestCase):
    def test_reads_messages(self):
        msgs = list(get_adapter("qq_txt").read(SAMPLE))
        self.assertEqual(len(msgs), 8)
        self.assertEqual(msgs[0].sender_name, "老王")
        self.assertEqual(msgs[0].sender_id, "12345678")
        self.assertEqual(msgs[0].content, "早上好")
        self.assertEqual(msgs[3].content, "[图片]")
        self.assertEqual(msgs[3].type, MsgType.IMAGE)

    def test_multiline_content(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
            fh.write("2024-01-15 09:00:00 甲(1)\n第一行\n第二行\n")
            path = fh.name
        try:
            msgs = list(get_adapter("qq_txt").read(path))
            self.assertEqual(msgs[0].content, "第一行\n第二行")
        finally:
            os.unlink(path)


class TestDedupe(unittest.TestCase):
    def test_removes_identical(self):
        msg = Message(timestamp=1, sender_id="a", sender_name="A", content="hi")
        self.assertEqual(len(dedupe([msg, msg])), 1)

    def test_same_second_same_length_different_content(self):
        a = Message(timestamp=1, sender_id="a", sender_name="A", content="abc")
        b = Message(timestamp=1, sender_id="a", sender_name="A", content="xyz")
        self.assertEqual(len(dedupe([a, b])), 2)


class TestExtract(unittest.TestCase):
    def test_extracts_actionable(self):
        msgs = list(get_adapter("qq_txt").read(SAMPLE))
        todos = extract_todos(msgs, session="技术交流群")
        texts = [t.text for t in todos]
        self.assertTrue(any("周报发我" in t for t in texts))
        self.assertTrue(any("报名下周的分享会" in t for t in texts))
        # 闲聊不应进入
        self.assertFalse(any("哈哈哈哈" in t for t in texts))

    def test_reason_is_recorded(self):
        msgs = [Message(timestamp=1, sender_id="a", sender_name="A",
                        content="记得明天提交报销单")]
        todo = extract_todos(msgs)[0]
        self.assertIn("行动词", todo.reason)
        self.assertGreaterEqual(todo.confidence, 0.5)


class TestStore(unittest.TestCase):
    def test_idempotent_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "t.db")
            msgs = list(get_adapter("qq_txt").read(SAMPLE))
            store = Store(db)
            try:
                first = store.add_messages(msgs)
                second = store.add_messages(msgs)
                self.assertEqual(first, 8)
                self.assertEqual(second, 0)
                self.assertEqual(store.stats()["messages"], 8)
            finally:
                store.close()

    def test_todos_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "t.db"))
            try:
                msgs = list(get_adapter("qq_txt").read(SAMPLE))
                todos = extract_todos(msgs, session="s")
                a = store.add_todos(todos)
                b = store.add_todos(todos)
                self.assertGreater(a, 0)
                self.assertEqual(b, 0)
            finally:
                store.close()


class TestMarkdown(unittest.TestCase):
    def test_render(self):
        msgs = [Message(timestamp=1700000000, sender_id="a", sender_name="A",
                        content="记得明天提交报销单")]
        md = render_markdown(extract_todos(msgs))
        self.assertIn("# 待办清单", md)
        self.assertIn("- [ ]", md)
        self.assertIn("依据", md)


if __name__ == "__main__":
    unittest.main()
