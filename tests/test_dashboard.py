"""看板数据层测试（不启动 GUI，可在无显示环境跑）。

A 阶段删除 tkinter 看板后，原 dashboard/ 里的纯数据函数
（load_items / filter_items / due_remindable / _extract_filenames）
迁到 core.dash_data / core.dash_files，这里继续覆盖它们。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from todoseine.adapters import get_adapter
from todoseine.core import Store, dedupe, extract_todos
from todoseine.core.dash_data import due_remindable, filter_items, load_items
from todoseine.core.dash_files import _extract_filenames
from todoseine.models import TodoItem

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

    def test_update_text(self) -> None:
        """看板行尾「改」走 update_text：改完能读回新内容，其它字段不动。"""
        store = Store(self.db)
        try:
            store.add_todo("改之前的文字")
            tid = [t for t in store.list_todos()
                   if t.text == "改之前的文字"][0].todo_id
            self.assertTrue(store.update_text(tid, "改之后的文字"))
        finally:
            store.close()
        items = load_items(self.db, "pending")
        texts = [item.text for item in items]
        self.assertIn("改之后的文字", texts)
        self.assertNotIn("改之前的文字", texts)

    def test_star_items_sort_to_top(self) -> None:
        """文档要求「按优先级从高到低」：★重点 必须浮到未完成条目最上面。"""
        store = Store(self.db)
        try:
            store.add_todo("普通的一条")
            store.add_todo("重点的一条")
            # ★重点 标记来自规则层（reason 含「★重点」），这里直接写库模拟
            store.conn.execute(
                "UPDATE todos SET reason='★重点' WHERE text='重点的一条'")
            store.conn.commit()
        finally:
            store.close()
        items = load_items(self.db, "pending")
        texts = [item.text for item in items]
        self.assertEqual(texts[0], "重点的一条")
        self.assertEqual(items[0].status, "pending")


class TestExtractFilenames(unittest.TestCase):
    """从待办文字 / 原文消息里识别 QQ 下载文件的文件名。"""

    def test_qq_file_prefix(self) -> None:
        self.assertEqual(_extract_filenames(["[文件] 周报.docx"]), ["周报.docx"])

    def test_filename_in_sentence(self) -> None:
        self.assertEqual(_extract_filenames(["把 报价单v2.xlsx 周五前发我"]),
                         ["报价单v2.xlsx"])

    def test_no_filename(self) -> None:
        self.assertEqual(_extract_filenames(["记得吃饭", "好的"]), [])

    def test_dedupe_and_order(self) -> None:
        self.assertEqual(
            _extract_filenames(["收 数据.csv", "再发一次 数据.csv 和 图.png"]),
            ["数据.csv", "图.png"])


class TestDueRemindable(unittest.TestCase):
    """到期提醒的挑选逻辑：快到期 / 刚过期的未完成条目，按到期升序。"""

    NOW = datetime(2026, 10, 27, 12, 0, 0)

    def _item(self, text, status="pending", tid=1):
        return TodoItem(text=text, session="s", sender="u",
                        message_ts=0, status=status, todo_id=tid)

    def test_picks_due_soon_and_overdue_sorted(self) -> None:
        items = [self._item("今天20点前交报告", tid=1),      # 8 小时后到期
                 self._item("10月26日交报告", tid=2),        # 已过期约 12 小时
                 self._item("没有期限的待办", tid=3)]
        pairs = due_remindable(items, self.NOW, within=9 * 3600)
        self.assertEqual([p[0].todo_id for p in pairs], [2, 1])   # 过期的排最前

    def test_skips_done_and_far_future(self) -> None:
        items = [self._item("今天12点半前交报告", status="done", tid=1),
                 self._item("下个月5号再说", tid=2)]
        self.assertEqual(due_remindable(items, self.NOW), [])

    def test_within_window(self) -> None:
        items = [self._item("今天14点前交报告", tid=1)]     # 2 小时后
        self.assertEqual(due_remindable(items, self.NOW, within=3600), [])
        self.assertEqual(len(due_remindable(items, self.NOW, within=3 * 3600)), 1)


class TestFilterItems(unittest.TestCase):
    """看板搜索框的过滤逻辑：大小写不敏感子串，匹配内容 / 发送人。"""

    def _item(self, text, sender="张三", tid=1):
        return TodoItem(text=text, session="s", sender=sender,
                        message_ts=0, status="pending", todo_id=tid)

    def test_empty_query_returns_all(self) -> None:
        items = [self._item("写周报", tid=1), self._item("买牛奶", tid=2)]
        self.assertEqual(len(filter_items(items, "")), 2)
        self.assertEqual(len(filter_items(items, "   ")), 2)

    def test_matches_text_case_insensitive(self) -> None:
        items = [self._item("把 ReportFinal.docx 发群里", tid=1),
                 self._item("写周报", tid=2)]
        hit = filter_items(items, "reportfinal")
        self.assertEqual([i.todo_id for i in hit], [1])

    def test_matches_sender(self) -> None:
        items = [self._item("写周报", sender="李四", tid=1),
                 self._item("买牛奶", sender="张三", tid=2)]
        hit = filter_items(items, "李四")
        self.assertEqual([i.todo_id for i in hit], [1])

    def test_no_match_returns_empty(self) -> None:
        items = [self._item("写周报", tid=1)]
        self.assertEqual(filter_items(items, "不存在的关键词"), [])


if __name__ == "__main__":
    unittest.main()
