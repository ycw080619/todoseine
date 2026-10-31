"""决策日志测试：extract_todos 出参 + store 落库/查询。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from todoseine.core import Store, extract_todos  # noqa: E402
from todoseine.models import Message, MsgType  # noqa: E402


def _msg(content, ts=1, sender_id="a", sender_name="A", msg_type=MsgType.TEXT):
    return Message(timestamp=ts, sender_id=sender_id, sender_name=sender_name,
                   content=content, type=msg_type)


class TestDecisionsOutParam(unittest.TestCase):
    def test_all_messages_recorded(self):
        msgs = [
            _msg("记得明天提交报销单"),    # pending
            _msg("今晚吃什么"),            # noise (score 0)
            _msg("需要确认一下时间", ts=2),  # unconfirmed (score 3? 行动词+需要)
        ]
        decisions = []
        extract_todos(msgs, decisions=decisions)
        self.assertEqual(len(decisions), 3)

    def test_outcomes_correct(self):
        msgs = [
            _msg("记得明天提交报销单"),            # 行动词+时间词 = 6 → pending
            _msg("哈哈哈哈好耶"),                  # 语气词+重复 → 负分 → noise
            _msg("需要大家配合一下", ts=2),        # 行动词=3，>=8字无短罚 → unconfirmed
        ]
        decisions = []
        extract_todos(msgs, decisions=decisions)
        outcomes = {d["outcome"] for d in decisions}
        self.assertIn("pending", outcomes)
        self.assertIn("noise", outcomes)
        self.assertIn("unconfirmed", outcomes)

    def test_filtered_outcome(self):
        """score 在 (0, weak_threshold) 之间 → filtered。"""
        msgs = [_msg("能不能帮我看看")]  # 疑问+指向我 = 3 → 刚好 weak_threshold=3 → unconfirmed
        # 用更低阈值让它变成 filtered
        decisions = []
        extract_todos(msgs, weak_threshold=5, todo_threshold=10, decisions=decisions)
        self.assertEqual(decisions[0]["outcome"], "filtered")

    def test_risk_is_noise(self):
        msgs = [_msg("[图片]", msg_type=MsgType.IMAGE)]
        decisions = []
        extract_todos(msgs, decisions=decisions)
        self.assertEqual(decisions[0]["outcome"], "noise")

    def test_no_decisions_param_unaffected(self):
        msgs = [_msg("记得提交")]
        todos = extract_todos(msgs)  # 不传 decisions
        self.assertEqual(len(todos), 1)


class TestStoreDecisions(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()
        os.unlink(self.db)

    def test_add_and_query(self):
        rows = [{
            "ts": 100, "sender_id": "a", "sender_name": "A",
            "content_hash": "abc", "outcome": "pending",
            "score": 6.0, "threshold": 4.0, "reason": "行动词:提交",
        }]
        n = self.store.add_decisions(rows)
        self.assertEqual(n, 1)
        d = self.store.query_decision(100, "a", "abc")
        self.assertIsNotNone(d)
        self.assertEqual(d["outcome"], "pending")
        self.assertEqual(d["score"], 6.0)


if __name__ == "__main__":
    unittest.main()
