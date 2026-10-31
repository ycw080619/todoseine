"""红包与转账识别测试。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from todoseine.core import extract_todos  # noqa: E402
from todoseine.core.classify import (  # noqa: E402
    classify, is_red_packet, is_transfer,
)
from todoseine.models import Message, MsgType  # noqa: E402


def _msg(content, sender="张三", ts=1, msg_type=MsgType.TEXT):
    return Message(timestamp=ts, sender_id="1", sender_name=sender,
                   content=content, type=msg_type)


class TestRedPacketDetect(unittest.TestCase):
    """红包判定 is_red_packet：类型标记与正文措辞两种形态都能识别。"""

    def test_type_marker(self):
        m = _msg("[红包] 恭喜发财，大吉大利")
        self.assertTrue(is_red_packet(m))
        self.assertEqual(classify(m), "redpacket")

    def test_qq_card_marker(self):
        self.assertEqual(classify(_msg("[QQ红包]")), "redpacket")

    def test_wording_only(self):
        """没有类型标记，只是正文里提到红包，也算。"""
        self.assertEqual(classify(_msg("群里发了个拼手气红包，快抢")), "redpacket")

    def test_transfer_not_treated_as_red_packet(self):
        """转账与红包是两条独立通道，互不误判。"""
        m = _msg("[转账] 500", msg_type=MsgType.TRANSFER)
        self.assertFalse(is_red_packet(m))
        self.assertTrue(is_transfer(m))
        self.assertEqual(classify(m), "transfer")

    def test_normal_text_not_red_packet(self):
        self.assertEqual(classify(_msg("明天记得交周报")), "chat")


class TestRedPacketExtract(unittest.TestCase):
    """红包抽取：生成高置信度条目，且不受 todo_threshold 影响。"""

    def test_generates_todo(self):
        todos = extract_todos([_msg("[红包] 恭喜发财")], session="群A")
        self.assertEqual(len(todos), 1)
        t = todos[0]
        self.assertEqual(t.reason, "红包提醒")
        self.assertEqual(t.status, "pending")
        self.assertEqual(t.confidence, 0.95)
        self.assertEqual(t.sender, "张三")
        self.assertIn("红包", t.text)

    def test_not_filtered_by_threshold(self):
        """红包是独立提醒通道：阈值抬到 99 也必须保留。"""
        todos = extract_todos([_msg("[红包] 恭喜发财")], todo_threshold=99,
                              weak_threshold=99)
        self.assertEqual(len(todos), 1)

    def test_decision_recorded_as_pending(self):
        decisions = []
        extract_todos([_msg("[红包] 恭喜发财")], decisions=decisions)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["outcome"], "pending")
        self.assertEqual(decisions[0]["reason"], "红包提醒")

    def test_multiple_red_packets(self):
        msgs = [_msg("[红包] 一号", sender="甲", ts=1),
                _msg("[QQ红包]", sender="乙", ts=2)]
        todos = extract_todos(msgs)
        self.assertEqual(len(todos), 2)
        self.assertEqual({t.sender for t in todos}, {"甲", "乙"})


class TestTransferDetect(unittest.TestCase):
    """转账判定 is_transfer：类型标记与收款措辞能认，自己的缴费请求不认。"""

    def test_type_marker(self):
        m = _msg("[转账] 500", msg_type=MsgType.TRANSFER)
        self.assertTrue(is_transfer(m))
        self.assertEqual(classify(m), "transfer")

    def test_wording_received(self):
        self.assertEqual(classify(_msg("已收款 200 元")), "transfer")

    def test_wording_transfer_to_me(self):
        self.assertEqual(classify(_msg("张三 向你转账 88 元")), "transfer")

    def test_own_payment_request_is_not_transfer(self):
        """「记得转账给房东」是自己的待办，不能被当成收到的转账。"""
        m = _msg("记得转账给房东")
        self.assertFalse(is_transfer(m))
        self.assertNotEqual(classify(m), "transfer")


class TestTransferExtract(unittest.TestCase):
    """转账抽取：生成高置信度条目，reason 与红包区分开。"""

    def test_generates_todo(self):
        todos = extract_todos([_msg("[转账] 500", sender="李四",
                                    msg_type=MsgType.TRANSFER)], session="群B")
        self.assertEqual(len(todos), 1)
        t = todos[0]
        self.assertEqual(t.reason, "转账提醒")
        self.assertEqual(t.status, "pending")
        self.assertEqual(t.confidence, 0.95)
        self.assertEqual(t.sender, "李四")
        self.assertIn("转账", t.text)

    def test_not_filtered_by_threshold(self):
        todos = extract_todos([_msg("[转账] 500", msg_type=MsgType.TRANSFER)],
                              todo_threshold=99, weak_threshold=99)
        self.assertEqual(len(todos), 1)

    def test_decision_recorded_as_pending(self):
        decisions = []
        extract_todos([_msg("[转账] 500", msg_type=MsgType.TRANSFER)],
                      decisions=decisions)
        self.assertEqual(decisions[0]["outcome"], "pending")
        self.assertEqual(decisions[0]["reason"], "转账提醒")

    def test_red_packet_and_transfer_coexist(self):
        """同一批消息里红包与转账各成一条，不会互相吞掉。"""
        msgs = [_msg("[红包] 恭喜发财", sender="甲", ts=1),
                _msg("[转账] 200", sender="乙", ts=2, msg_type=MsgType.TRANSFER)]
        todos = extract_todos(msgs)
        self.assertEqual(len(todos), 2)
        self.assertEqual({t.reason for t in todos}, {"红包提醒", "转账提醒"})


if __name__ == "__main__":
    unittest.main()
