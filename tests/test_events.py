"""跨群事件合并测试。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from chat2todo.core.events import (  # noqa: E402
    _compatible, _extract_action, _jaccard, _object_bigrams,
    extract_event_key, merge_events,
)
from chat2todo.models import Message, TodoItem  # noqa: E402


def _todo(text, ts=1, sender="A", chat_id="", confidence=0.8):
    return TodoItem(text=text, session="s", sender=sender, message_ts=ts,
                    reason="行动词:报名", confidence=confidence, status="pending",
                    chat_id=chat_id)


class TestExtractAction(unittest.TestCase):
    """动作归一 _extract_action：覆盖全部 10 个类别、优先级取首命中、无动作返回 None。"""
    def test_all_action_categories(self):
        """每个动作归一类别都能命中。"""
        cases = [
            ("请报名参加", "signup"),
            ("记得签到", "signup"),
            ("需要缴费", "pay"),
            ("明天提交周报", "submit"),
            ("下周期末考试", "exam"),
            ("下午开会", "meeting"),
            ("今天值班", "duty"),
            ("请填写表格", "fill"),
            ("去领取物资", "claim"),
            ("晚上彩排", "rehearse"),
            ("明天答辩", "compete"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(_extract_action(text), expected)

    def test_action_priority_first_match(self):
        """ACTION_MAP 顺序即优先级，取第一个命中。"""
        # 报名在前、提交在后，应返回 signup 而非 submit
        self.assertEqual(_extract_action("报名后提交材料"), "signup")

    def test_no_action_returns_none(self):
        self.assertIsNone(_extract_action("今天天气真好"))


class TestObjectBigrams(unittest.TestCase):
    """对象 bigram 提取 _object_bigrams：验证 _STRIP_WORDS 剔除、短文本空集、全量 bigram 生成。"""
    def test_strips_action_and_time_words(self):
        bigrams = _object_bigrams("请大家记得确认时间")
        # "请"/"大家"/"记得"/"确认" 都在 _STRIP_WORDS 里，剔除后剩 "时间"
        self.assertEqual(bigrams, {"时间"})

    def test_short_text_returns_empty(self):
        # 剔除后不足 2 字
        self.assertEqual(_object_bigrams("记得"), set())

    def test_empty_string(self):
        self.assertEqual(_object_bigrams(""), set())

    def test_generates_all_bigrams(self):
        bigrams = _object_bigrams("报销单填写")
        self.assertEqual(bigrams, {"报销", "销单", "单填", "填写"})


class TestJaccard(unittest.TestCase):
    """Jaccard 相似度 _jaccard：空集、完全相同、部分重叠三种边界。"""
    def test_empty_sets(self):
        self.assertEqual(_jaccard(set(), {"a"}), 0.0)
        self.assertEqual(_jaccard({"a"}, set()), 0.0)

    def test_identical(self):
        self.assertEqual(_jaccard({"a", "b"}, {"a", "b"}), 1.0)

    def test_partial(self):
        # inter=1, union=3 → 1/3
        self.assertAlmostEqual(_jaccard({"a", "b"}, {"b", "c"}), 1 / 3)


def _key(action=None, deadline=None, bigrams=None):
    return {"action": action, "deadline": deadline,
            "bigrams": bigrams if bigrams is not None else set()}


class TestCompatible(unittest.TestCase):
    """事件兼容判定 _compatible：覆盖 action/deadline 不一致、三种锚点等级下的 bigram 重叠阈值。"""
    def test_action_differs(self):
        a = _key(action="signup", bigrams={"x", "y", "z"})
        b = _key(action="submit", bigrams={"x", "y", "z"})
        self.assertFalse(_compatible(a, b))

    def test_deadline_differs(self):
        a = _key(action="signup", deadline=100, bigrams={"x", "y", "z"})
        b = _key(action="signup", deadline=200, bigrams={"x", "y", "z"})
        self.assertFalse(_compatible(a, b))

    def test_action_and_deadline_shared_ge_3(self):
        """有 action+deadline 锚点，共享 bigram ≥ 3 即兼容。"""
        bigrams = {"a", "b", "c", "d"}
        a = _key(action="signup", deadline=100, bigrams=bigrams)
        b = _key(action="signup", deadline=100, bigrams={"a", "b", "c", "e"})
        self.assertTrue(_compatible(a, b))

    def test_action_and_deadline_jaccard_ge_half(self):
        """有锚点时 Jaccard ≥ 0.5 也兼容（共享 2、并集 4 → 0.5）。"""
        a = _key(action="signup", deadline=100, bigrams={"a", "b", "c"})
        b = _key(action="signup", deadline=100, bigrams={"a", "b", "d"})
        self.assertTrue(_compatible(a, b))

    def test_action_only_needs_shared_ge_3(self):
        """只有 action 锚点，需共享 ≥ 3。"""
        a = _key(action="signup", bigrams={"a", "b", "c", "d"})
        b = _key(action="signup", bigrams={"a", "b", "c", "e"})
        self.assertTrue(_compatible(a, b))

    def test_action_only_shared_lt_3_false(self):
        a = _key(action="signup", bigrams={"a", "b", "c", "d"})
        b = _key(action="signup", bigrams={"a", "x", "y", "z"})
        self.assertFalse(_compatible(a, b))

    def test_no_anchor_needs_shared_ge_5(self):
        """无锚点（action/deadline 都空），需共享 ≥ 5。"""
        shared = {"a", "b", "c", "d", "e"}
        a = _key(bigrams=shared | {"x"})
        b = _key(bigrams=shared | {"y"})
        self.assertTrue(_compatible(a, b))

    def test_no_anchor_shared_lt_5_false(self):
        a = _key(bigrams={"a", "b", "c", "d", "x"})
        b = _key(bigrams={"a", "b", "c", "d", "y"})
        self.assertFalse(_compatible(a, b))


class TestExtractEventKey(unittest.TestCase):
    """事件键提取 extract_event_key：action 归一、deadline 解析、无动作、bigram 非空。"""
    def test_action_signup(self):
        key = extract_event_key("大家记得报名下周的分享会")
        self.assertEqual(key["action"], "signup")

    def test_action_submit(self):
        key = extract_event_key("明天上午十点前提交周报")
        self.assertEqual(key["action"], "submit")

    def test_deadline_parsed(self):
        key = extract_event_key("10月30号前提交报名表")
        self.assertIsNotNone(key["deadline"])

    def test_no_action(self):
        key = extract_event_key("今天天气不错")
        self.assertIsNone(key["action"])

    def test_bigrams_present(self):
        key = extract_event_key("提交报销单")
        self.assertIsInstance(key["bigrams"], set)
        self.assertGreater(len(key["bigrams"]), 0)


class TestMergeEvents(unittest.TestCase):
    """跨群合并 merge_events：覆盖同/异群、动作/截止日分歧、多群合并、置信度与时间戳选主、空输入与回退。"""
    def test_cross_group_same_event_merges(self):
        """两个不同群的同一条报名通知 → 合并为 1 条。"""
        msgs = [
            Message(timestamp=1, sender_id="a", sender_name="A",
                    content="大家记得报名下周的分享会", chat_id="群1"),
            Message(timestamp=2, sender_id="b", sender_name="B",
                    content="记得报名下周的分享会", chat_id="群2"),
        ]
        todos = [
            _todo("大家记得报名下周的分享会", ts=1, chat_id="群1"),
            _todo("记得报名下周的分享会", ts=2, chat_id="群2"),
        ]
        merged, note = merge_events(todos, msgs)
        self.assertEqual(len(merged), 1)
        self.assertIn("跨群合并", merged[0].reason)
        self.assertIn("2", merged[0].reason)
        self.assertIn("跨群合并", note)

    def test_same_group_not_merged(self):
        """同群内的两条不合并（交给 vector_pass 处理）。"""
        msgs = [
            Message(timestamp=1, sender_id="a", sender_name="A",
                    content="报名下周的分享会", chat_id="群1"),
            Message(timestamp=2, sender_id="b", sender_name="B",
                    content="报名下周的分享会", chat_id="群1"),
        ]
        todos = [
            _todo("报名下周的分享会", ts=1, chat_id="群1"),
            _todo("报名下周的分享会", ts=2, chat_id="群1"),
        ]
        merged, note = merge_events(todos, msgs)
        self.assertEqual(len(merged), 2)
        self.assertEqual(note, "")

    def test_both_chat_id_empty_not_merged(self):
        """两条 chat_id 都为空 → 视为同群，不合并。"""
        msgs = [
            Message(timestamp=1, sender_id="a", sender_name="A",
                    content="报名下周的分享会", chat_id=""),
            Message(timestamp=2, sender_id="b", sender_name="B",
                    content="报名下周的分享会", chat_id=""),
        ]
        todos = [
            _todo("报名下周的分享会", ts=1, chat_id=""),
            _todo("报名下周的分享会", ts=2, chat_id=""),
        ]
        merged, _ = merge_events(todos, msgs)
        self.assertEqual(len(merged), 2)

    def test_different_action_not_merged(self):
        msgs = [
            Message(timestamp=1, sender_id="a", sender_name="A",
                    content="报名下周的分享会", chat_id="群1"),
            Message(timestamp=2, sender_id="b", sender_name="B",
                    content="提交下周的分享会材料", chat_id="群2"),
        ]
        todos = [
            _todo("报名下周的分享会", ts=1, chat_id="群1"),
            _todo("提交下周的分享会材料", ts=2, chat_id="群2"),
        ]
        merged, _ = merge_events(todos, msgs)
        self.assertEqual(len(merged), 2)

    def test_different_deadline_not_merged(self):
        """同一动作但截止日不同 → 不合并。"""
        msgs = [
            Message(timestamp=1, sender_id="a", sender_name="A",
                    content="10月30号前提交报名表", chat_id="群1"),
            Message(timestamp=2, sender_id="b", sender_name="B",
                    content="11月5号前提交报名表", chat_id="群2"),
        ]
        todos = [
            _todo("10月30号前提交报名表", ts=1, chat_id="群1"),
            _todo("11月5号前提交报名表", ts=2, chat_id="群2"),
        ]
        merged, _ = merge_events(todos, msgs)
        self.assertEqual(len(merged), 2)

    def test_three_groups_same_event_merges_to_one(self):
        """三个不同群的同一件事 → 合并为 1 条，标记 3 群。"""
        msgs = [
            Message(timestamp=1, sender_id="a", sender_name="A",
                    content="报名下周的分享会", chat_id="群1"),
            Message(timestamp=2, sender_id="b", sender_name="B",
                    content="报名下周的分享会", chat_id="群2"),
            Message(timestamp=3, sender_id="c", sender_name="C",
                    content="报名下周的分享会", chat_id="群3"),
        ]
        todos = [
            _todo("报名下周的分享会", ts=1, chat_id="群1"),
            _todo("报名下周的分享会", ts=2, chat_id="群2"),
            _todo("报名下周的分享会", ts=3, chat_id="群3"),
        ]
        merged, note = merge_events(todos, msgs)
        self.assertEqual(len(merged), 1)
        self.assertIn("3", merged[0].reason)
        self.assertIn("跨群合并：2", note)  # 3 条合成 1 条，合并了 2 条

    def test_keeps_higher_confidence(self):
        msgs = [
            Message(timestamp=1, sender_id="a", sender_name="A",
                    content="报名下周的分享会", chat_id="群1"),
            Message(timestamp=2, sender_id="b", sender_name="B",
                    content="报名下周的分享会", chat_id="群2"),
        ]
        todos = [
            _todo("报名下周的分享会", ts=1, chat_id="群1", confidence=0.6),
            _todo("报名下周的分享会", ts=2, chat_id="群2", confidence=0.9),
        ]
        merged, _ = merge_events(todos, msgs)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].confidence, 0.9)

    def test_confidence_tie_keeps_earlier(self):
        """置信度相同时保留时间戳更小的（更早的）。"""
        msgs = [
            Message(timestamp=10, sender_id="a", sender_name="A",
                    content="报名下周的分享会", chat_id="群1"),
            Message(timestamp=20, sender_id="b", sender_name="B",
                    content="报名下周的分享会", chat_id="群2"),
        ]
        todos = [
            _todo("报名下周的分享会", ts=10, chat_id="群1", confidence=0.8),
            _todo("报名下周的分享会", ts=20, chat_id="群2", confidence=0.8),
        ]
        merged, _ = merge_events(todos, msgs)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].message_ts, 10)

    def test_single_todo_unchanged(self):
        msgs = [Message(timestamp=1, sender_id="a", sender_name="A",
                        content="报名下周的分享会", chat_id="群1")]
        todos = [_todo("报名下周的分享会", ts=1, chat_id="群1")]
        merged, note = merge_events(todos, msgs)
        self.assertEqual(len(merged), 1)
        self.assertEqual(note, "")

    def test_empty_todos_unchanged(self):
        merged, note = merge_events([], [])
        self.assertEqual(merged, [])
        self.assertEqual(note, "")

    def test_fallback_to_todo_text_when_no_message(self):
        """找不到源消息时用 todo.text 算事件键。"""
        msgs = []  # 不传消息
        todos = [
            _todo("报名下周的分享会", ts=1, chat_id="群1"),
            _todo("报名下周的分享会", ts=2, chat_id="群2"),
        ]
        merged, _ = merge_events(todos, msgs)
        self.assertEqual(len(merged), 1)

    def test_mixed_merge_and_keep(self):
        """两条可合并 + 一条独立的 → 输出 2 条。"""
        msgs = [
            Message(timestamp=1, sender_id="a", sender_name="A",
                    content="报名下周的分享会", chat_id="群1"),
            Message(timestamp=2, sender_id="b", sender_name="B",
                    content="报名下周的分享会", chat_id="群2"),
            Message(timestamp=3, sender_id="c", sender_name="C",
                    content="明天交水电费", chat_id="群3"),
        ]
        todos = [
            _todo("报名下周的分享会", ts=1, chat_id="群1"),
            _todo("报名下周的分享会", ts=2, chat_id="群2"),
            _todo("明天交水电费", ts=3, chat_id="群3", confidence=0.7),
        ]
        merged, _ = merge_events(todos, msgs)
        self.assertEqual(len(merged), 2)


if __name__ == "__main__":
    unittest.main()

