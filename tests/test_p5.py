"""P5：微信适配器与 AI 环节接线的测试。

看板/CLI 之外的这两块最容易「写了没接」，所以各配一条端到端断言。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from todoseine.adapters import get_adapter  # noqa: E402
from todoseine.cli import main  # noqa: E402
from todoseine.core import Store  # noqa: E402

SAMPLE = """2024-01-15 09:15:00 小李: 明天下午三点前把周报发我
2024-01-15 09:20:00 小张: 我明天自己把方案改了
[2024-01-15 09:30:00] 小王: 记得报名下周的分享会，截止周五
"""


class TestWeChatAdapter(unittest.TestCase):
    def test_parses_three_header_styles(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(SAMPLE)
            msgs = list(get_adapter("wechat_txt").read(path))
        finally:
            os.remove(path)
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0].sender_name, "小李")
        self.assertIn("周报", msgs[0].content)
        self.assertEqual(msgs[2].sender_name, "小王")
        self.assertIn("分享会", msgs[2].content)

    def test_multiline_body(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("2024-01-15 09:15:00 小李\n第一行\n第二行\n")
            msgs = list(get_adapter("wechat_txt").read(path))
        finally:
            os.remove(path)
        self.assertEqual(len(msgs), 1)
        self.assertIn("第二行", msgs[0].content)


class _StubAI:
    """受控替身：只打开 intent/merge，不发网络请求。"""

    filter_out = ""

    def __init__(self, cfg, on_call=None) -> None:
        self.enabled = True
        self.usable = True
        self.calls = 1

    def on(self, feature: str) -> bool:
        return feature in ("intent", "merge")

    def intent(self, text: str, session: str = "") -> bool:
        # 注意：空字符串 is in 任何字符串，所以必须先判空再判断包含
        if not self.filter_out:
            return True
        return self.filter_out not in text

    def merge(self, texts, session: str = ""):
        return ["合并后的一条待办"] if len(texts) > 1 else None

    def review(self, text, session: str = ""):
        return True

    def rewrite(self, text, session: str = ""):
        return None


class TestAIWiring(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        fd, self.txt = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(SAMPLE)

    def tearDown(self) -> None:
        for p in (self.db, self.txt):
            try:
                os.remove(p)
            except OSError:
                pass

    def _run(self, stub_filter: str = "") -> None:
        _StubAI.filter_out = stub_filter
        with mock.patch("todoseine.cli.AI", _StubAI), redirect_stdout(io.StringIO()):
            code = main(["import", self.txt, "--adapter", "wechat_txt", "--db", self.db])
        self.assertEqual(code, 0)

    def test_intent_filters_self_talk(self) -> None:
        """「我明天自己改方案」是说话人自己的事，应被意图过滤掉。"""
        self._run(stub_filter="自己")
        store = Store(self.db)
        try:
            texts = [t.text for t in store.list_todos()]
        finally:
            store.close()
        self.assertTrue(all("自己" not in t for t in texts))
        self.assertTrue(texts)

    def test_merge_rebuilds_list(self) -> None:
        self._run()
        store = Store(self.db)
        try:
            todos = store.list_todos()
        finally:
            store.close()
        self.assertEqual([t.text for t in todos], ["合并后的一条待办"])
        self.assertEqual(todos[0].confidence, 0.70)


if __name__ == "__main__":
    unittest.main()
