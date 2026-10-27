"""核心逻辑单元测试。运行：python -m unittest discover -s tests"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from chat2todo.adapters import get_adapter  # noqa: E402
from chat2todo.core import Store, dedupe, extract_todos, render_markdown, to_csv, to_json  # noqa: E402
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

    def test_chat_names_roundtrip(self):
        """群号 → 群名：写入、覆盖、跳过空名。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "t.db"))
            try:
                n = store.set_chat_names({"116316805": "C#技术交流",
                                          "10001": "", "": "匿名"})
                self.assertEqual(n, 1)
                self.assertEqual(store.chat_name_map(),
                                 {"116316805": "C#技术交流"})
                store.set_chat_names({"116316805": "C# 技术交流·新"})
                self.assertEqual(store.chat_name_map()["116316805"],
                                 "C# 技术交流·新")
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


class TestP2SearchAndExport(unittest.TestCase):
    """P2：检索、双视图上下文、三种导出格式。"""

    def _seeded(self, tmp: str) -> Store:
        store = Store(os.path.join(tmp, "p2.db"))
        msgs = list(get_adapter("qq_txt").read(SAMPLE))
        store.add_messages(msgs, session="s")
        store.add_todos(extract_todos(msgs, session="s"))
        return store

    def test_search_hits_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._seeded(tmp)
            try:
                hits = store.search("周报")
                self.assertTrue(hits, "应能检索到包含『周报』的消息")
                self.assertIn("周报", hits[0]["content"])
            finally:
                store.close()

    def test_search_miss_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._seeded(tmp)
            try:
                self.assertEqual(store.search("绝对不存在的词xyz"), [])
            finally:
                store.close()

    def test_context_around_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._seeded(tmp)
            try:
                first = store.all_messages()[0]
                target = store.search("周报")[0]["id"]
                rows = store.context(target, before=2, after=2)
                self.assertGreaterEqual(len(rows), 1)
                self.assertIn(target, [r["id"] for r in rows])
                self.assertEqual(first.sender_name, "老王")
            finally:
                store.close()

    def test_export_csv_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._seeded(tmp)
            try:
                todos = store.list_todos()
                csv_text = to_csv(todos)
                self.assertTrue(csv_text.startswith("id,text,session,sender,ts"))
                self.assertEqual(len(csv_text.strip().splitlines()), len(todos) + 1)
                json_text = to_json(todos)
                self.assertIn('"generator": "chat2todo"', json_text)
                self.assertIn('"todos"', json_text)
            finally:
                store.close()


class TestChatPurge(unittest.TestCase):
    """P1：消息记录来源群，并按群清理历史。"""

    def _store(self, tmp: str) -> Store:
        return Store(os.path.join(tmp, "chat.db"))

    def test_messages_keep_chat_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            try:
                store.add_messages([
                    Message(timestamp=1000, sender_id="1", sender_name="A",
                            content="记得交周报", chat_id="111"),
                    Message(timestamp=2000, sender_id="2", sender_name="B",
                            content="闲聊", chat_id="222"),
                ], session="qq")
                chats = {r["chat_id"]: r["n"] for r in store.list_chats()}
                self.assertEqual(chats, {"111": 1, "222": 1})
            finally:
                store.close()

    def test_backfill_chat_id_on_resync(self):
        """老库没有 chat_id，重新同步同一条消息时应补上来源群。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            try:
                store.add_messages([Message(timestamp=1000, sender_id="1",
                                            sender_name="A", content="记得交周报")])
                store.add_messages([Message(timestamp=1000, sender_id="1",
                                            sender_name="A", content="记得交周报",
                                            chat_id="111")])
                chats = store.list_chats()
                self.assertEqual(len(chats), 1)
                self.assertEqual(chats[0]["chat_id"], "111")
            finally:
                store.close()

    def test_migrate_old_messages_table(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                "sender_id TEXT NOT NULL, sender_name TEXT NOT NULL, "
                "content TEXT NOT NULL, type INTEGER NOT NULL DEFAULT 0, "
                "content_len INTEGER NOT NULL, content_hash TEXT NOT NULL, "
                "session TEXT NOT NULL DEFAULT '', "
                "UNIQUE (ts, sender_id, content_len, content_hash))"
            )
            conn.commit()
            conn.close()
            store = Store(path)
            try:
                cols = {r[1] for r in store.conn.execute("PRAGMA table_info(messages)")}
                self.assertIn("chat_id", cols)
            finally:
                store.close()

    def test_purge_chat_removes_messages_and_todos(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            try:
                msgs = [
                    Message(timestamp=1000, sender_id="1", sender_name="A",
                            content="记得交周报", chat_id="111"),
                    Message(timestamp=2000, sender_id="2", sender_name="B",
                            content="明天开会对齐需求", chat_id="222"),
                ]
                store.add_messages(msgs, session="qq")
                store.add_todos(extract_todos(msgs, session="qq"))
                total_todos = len(store.list_todos())
                hit = store.chat_counts("222")
                self.assertEqual(hit["messages"], 1)
                self.assertGreaterEqual(hit["todos"], 1)

                res = store.purge_chat("222")
                self.assertEqual(res["messages"], 1)
                self.assertEqual(res["todos"], hit["todos"])
                self.assertEqual(len(store.list_todos()), total_todos - hit["todos"])
                self.assertEqual(store.chat_counts("111")["messages"], 1)
                self.assertEqual(store.search("需求"), [])
            finally:
                store.close()

    def test_backfill_only_touches_unlabeled_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            try:
                store.add_messages([
                    Message(timestamp=1000, sender_id="1", sender_name="A",
                            content="记得交周报"),
                    Message(timestamp=2000, sender_id="2", sender_name="B",
                            content="明天开会", chat_id="222"),
                ])
                filled = store.backfill_chat_ids([
                    (1000, "1", len("记得交周报"), Message(timestamp=1000, sender_id="1",
                                                       sender_name="A",
                                                       content="记得交周报").content_hash,
                     "111"),
                    (2000, "2", len("明天开会"), Message(timestamp=2000, sender_id="2",
                                                      sender_name="B",
                                                      content="明天开会").content_hash,
                     "999"),
                ])
                self.assertEqual(filled, 1)
                chats = {r["chat_id"]: r["n"] for r in store.list_chats()}
                self.assertEqual(chats, {"111": 1, "222": 1})
            finally:
                store.close()

    def test_purge_keep_todos(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            try:
                msgs = [Message(timestamp=1000, sender_id="1", sender_name="A",
                                content="记得交周报", chat_id="111")]
                store.add_messages(msgs, session="qq")
                store.add_todos(extract_todos(msgs, session="qq"))
                total = len(store.list_todos())
                res = store.purge_chat("111", with_todos=False)
                self.assertEqual(res, {"messages": 1, "todos": 0})
                self.assertEqual(len(store.list_todos()), total)
            finally:
                store.close()

    def test_purge_unmarked(self):
        """未标记消息清理：只删 chat_id 为空的消息，待办与已标记消息不动。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            try:
                msgs = [
                    Message(timestamp=1000, sender_id="1", sender_name="A",
                            content="记得交周报"),
                    Message(timestamp=2000, sender_id="2", sender_name="B",
                            content="明天开会", chat_id="222"),
                ]
                store.add_messages(msgs, session="qq")
                store.add_todos(extract_todos(msgs, session="qq"))
                self.assertEqual(store.unmarked_counts()["messages"], 1)
                total_todos = len(store.list_todos())

                res = store.purge_unmarked()
                self.assertEqual(res["messages"], 1)
                self.assertEqual(store.unmarked_counts()["messages"], 0)
                self.assertEqual(len(store.list_todos()), total_todos)
                self.assertEqual(store.chat_counts("222")["messages"], 1)
                self.assertEqual(store.search("周报"), [])
                self.assertEqual(store.purge_unmarked()["messages"], 0)
            finally:
                store.close()


class TestWeeklyStats(unittest.TestCase):
    """每周摘要：近 7 天新增 / 完成 / 最活跃群（群名优先，查不到用号码）。"""

    NOW = datetime(2026, 10, 27, 12, 0, 0)
    TS_NOW = int(NOW.timestamp())

    def _store(self, tmp: str) -> Store:
        return Store(os.path.join(tmp, "chat.db"))

    def test_counts_and_top_chat_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            try:
                store.add_messages([
                    Message(timestamp=self.TS_NOW - 1000, sender_id="1",
                            sender_name="A", content="消息1", chat_id="111"),
                    Message(timestamp=self.TS_NOW - 2000, sender_id="2",
                            sender_name="B", content="消息2", chat_id="111"),
                    Message(timestamp=self.TS_NOW - 3000, sender_id="3",
                            sender_name="C", content="消息3", chat_id="222"),
                    # 8 天前的老消息不计入
                    Message(timestamp=self.TS_NOW - 8 * 86400, sender_id="1",
                            sender_name="A", content="老消息", chat_id="333"),
                ], session="qq")
                store.set_chat_names({"111": "产品交流群"})
                # 近 7 天新增 2 条待办（ts 落在窗口内），1 条老的
                store.conn.executemany(
                    "INSERT INTO todos (text, session, sender, ts, status) "
                    "VALUES (?, '', '', ?, 'pending')",
                    [("新待办1", self.TS_NOW - 100), ("新待办2", self.TS_NOW - 200),
                     ("老待办", self.TS_NOW - 8 * 86400)])
                # 完成：一条今天勾掉的 + 一条已归档进回收站的 + 一条 8 天前完成的
                store.conn.execute(
                    "INSERT INTO todos (text, session, sender, ts, status, done_at) "
                    "VALUES ('今天完成', '', '', ?, 'done', '2026-10-27')",
                    (self.TS_NOW - 100,))
                store.conn.execute(
                    "INSERT INTO trash (text, session, sender, ts, status, done_at, "
                    "deleted_at) VALUES ('归档的', '', '', ?, 'done', '2026-10-25', "
                    "'2026-10-26')", (self.TS_NOW - 500,))
                store.conn.execute(
                    "INSERT INTO todos (text, session, sender, ts, status, done_at) "
                    "VALUES ('上周完成', '', '', ?, 'done', '2026-10-18')",
                    (self.TS_NOW - 9 * 86400,))
                store.conn.commit()

                s = store.weekly_stats(self.NOW)
                self.assertEqual(s["new"], 3)          # 含今天完成的那条（按 ts 算新增）
                self.assertEqual(s["done"], 2)         # 今天完成 + 回收站里归档的
                self.assertEqual(s["top_chat"], ("产品交流群", 2))
            finally:
                store.close()

    def test_empty_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            try:
                s = store.weekly_stats(self.NOW)
                self.assertEqual(s, {"new": 0, "done": 0, "top_chat": None})
            finally:
                store.close()

    def test_top_chat_falls_back_to_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            try:
                store.add_messages([
                    Message(timestamp=self.TS_NOW - 1000, sender_id="1",
                            sender_name="A", content="消息1", chat_id="999"),
                ], session="qq")
                s = store.weekly_stats(self.NOW)
                self.assertEqual(s["top_chat"], ("999", 1))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
