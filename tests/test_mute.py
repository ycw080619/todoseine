"""「以后不再通知」静音规则：落库、生效范围、撤销恢复。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from todoseine.core import Store  # noqa: E402
from todoseine.models import TodoItem  # noqa: E402


def _todo(text: str, session: str, sender: str, status: str = "pending") -> TodoItem:
    return TodoItem(text=text, session=session, sender=sender, message_ts=1,
                    reason="测试", confidence=0.9, status=status)


class TestMute(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self._tmp.name, "t.db"))

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_mute_marks_existing_todos(self) -> None:
        self.store.add_todos([
            _todo("交材料", "qq", "老王"),
            _todo("开会", "qq", "老王"),
            _todo("报名", "qq", "小李"),
        ])
        res = self.store.mute("qq", "老王")
        self.assertTrue(res["created"])
        self.assertEqual(res["muted_todos"], 2)

        status = {t.text: t.status for t in self.store.list_todos()}
        self.assertEqual(status["交材料"], "muted")
        self.assertEqual(status["开会"], "muted")
        self.assertEqual(status["报名"], "pending")

    def test_semantic_mute_hits_same_kind(self) -> None:
        """「此类消息」：静音一条通知，同批模板通知都被收起，无关闲聊不受影响。"""
        seed = "📣【团组织生活会通知】请全体团员于本周四下午三点到活动室参加组织生活会"
        self.store.add_todos([
            _todo(seed, "qq", "班长"),
            _todo("【青年大学习通知】请全体团员本周五前完成青年大学习并提交截图",
                  "qq", "团支书"),
            _todo("【班会通知】请全体同学今晚七点到教室开班会", "qq", "班长"),
            _todo("我电脑之前3200买的", "qq", "小明"),
            _todo("明天下午三点前把材料交给老王", "qq", "小明"),
        ])
        res = self.store.mute("qq", "", seed)
        self.assertEqual(res["rule_id"] > 0, True)
        self.assertEqual(res["muted_todos"], 3)

        status = {t.text: t.status for t in self.store.list_todos()}
        self.assertEqual(status[seed], "muted")
        self.assertEqual(status["【青年大学习通知】请全体团员本周五前完成青年大学习并提交截图"],
                         "muted")
        self.assertEqual(status["我电脑之前3200买的"], "pending")
        self.assertEqual(status["明天下午三点前把材料交给老王"], "pending")

    def test_semantic_mute_applies_to_new_todos(self) -> None:
        self.store.mute("qq", "", "【会议通知】请各位同学明天上午九点到会议室开会")
        self.store.add_todos([
            _todo("【会议通知】请全体成员本周五下午两点到会议室开会", "qq", "班长"),
            _todo("晚上吃啥", "qq", "小明"),
        ])
        status = {t.text: t.status for t in self.store.list_todos()}
        self.assertEqual(status["【会议通知】请全体成员本周五下午两点到会议室开会"], "muted")
        self.assertEqual(status["晚上吃啥"], "pending")

    def test_semantic_rule_survives_unmute_of_others(self) -> None:
        """撤销窄规则不会顺手解掉语义静音。"""
        seed = "【报名通知】请各班于本周五前把参赛名单交给辅导员"
        self.store.add_todos([
            _todo(seed, "qq", "班长"),
            _todo("【报名通知】请各班于下周一前把参赛名单交给辅导员", "qq", "班长"),
        ])
        self.store.mute("qq", "班长")          # 按人
        kind = self.store.mute("qq", "", seed)  # 按类
        res = self.store.unmute(kind["rule_id"])
        self.assertEqual(res["restored"], 0)   # 按人规则仍然压着
        self.assertEqual(self.store.stats()["muted"], 2)

    def test_matches_seed_threshold(self) -> None:
        from todoseine.core.store import MUTE_MIN_CHARS, MUTE_SIM_THRESHOLD

        self.assertGreater(MUTE_SIM_THRESHOLD, 0)
        hit, _ = Store.matches_seed("短句", [])
        self.assertFalse(hit)
        hit, _ = Store.matches_seed("短句" * MUTE_MIN_CHARS, [])
        self.assertFalse(hit)   # 没有样板时永不命中

    def test_new_todos_land_muted(self) -> None:
        self.store.mute("qq", "老王")
        added = self.store.add_todos([_todo("新通知", "qq", "老王")])
        self.assertEqual(added, 1)
        self.assertEqual(self.store.list_todos()[0].status, "muted")

    def test_whole_source_scope(self) -> None:
        self.store.add_todos([_todo("a", "qq", "老王"), _todo("b", "qq", "小李")])
        res = self.store.mute("qq")
        self.assertEqual(res["muted_todos"], 2)
        self.assertEqual(self.store.stats()["muted"], 2)

    def test_unmute_restores(self) -> None:
        self.store.add_todos([_todo("交材料", "qq", "老王")])
        rule_id = self.store.mute("qq", "老王")["rule_id"]
        res = self.store.unmute(rule_id)
        self.assertTrue(res["found"])
        self.assertEqual(res["restored"], 1)
        self.assertEqual(self.store.list_todos()[0].status, "pending")
        self.assertEqual(self.store.list_mutes(), [])

    def test_unmute_missing_rule(self) -> None:
        self.assertFalse(self.store.unmute(999)["found"])

    def test_release_specific_todo(self) -> None:
        self.store.add_todos([_todo("交材料", "qq", "老王"),
                              _todo("开会", "qq", "老王"),
                              _todo("报名", "qq", "小李")])
        self.store.mute("qq", "老王")
        restored = self.store.release("qq", "老王")
        self.assertEqual(restored, 2)
        self.assertEqual(self.store.stats()["pending"], 3)

    def test_release_semantic_by_text(self) -> None:
        seed = "【会议通知】请各位同学明天上午九点到会议室开会"
        self.store.add_todos([_todo(seed, "qq", "班长")])
        self.store.mute("qq", "", seed)
        self.assertEqual(self.store.stats()["muted"], 1)
        restored = self.store.release("qq", "班长", seed)
        self.assertEqual(restored, 1)
        self.assertEqual(self.store.stats()["pending"], 1)

    def test_release_without_rule(self) -> None:
        self.assertEqual(self.store.release("qq", "张三"), 0)

    def test_is_muted_wildcards(self) -> None:
        rules = [("", "老王"), ("qq", "")]
        self.assertTrue(Store.is_muted("qq", "老王", rules))
        self.assertTrue(Store.is_muted("wx", "老王", rules))
        self.assertTrue(Store.is_muted("qq", "小李", rules))
        self.assertFalse(Store.is_muted("wx", "小李", rules))

    def test_empty_scope_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.mute("", "")


if __name__ == "__main__":
    unittest.main()
