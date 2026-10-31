"""核心逻辑单元测试。运行：python -m unittest discover -s tests"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from todoseine.adapters import get_adapter  # noqa: E402
from todoseine.core import Store, dedupe, extract_todos, render_markdown, to_csv, to_json  # noqa: E402
from todoseine.models import Message, MsgType  # noqa: E402

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

    def test_thresholds_from_config_override_constants(self):
        """回归：todo_threshold/pending_threshold 必须真正影响抽取，
        否则用户在 c2t.toml 里改阈值完全不生效（之前硬编码常量）。"""
        # "记得提交月度报表" 得分 5（行动词 + 通知/要求），高于默认 todo(4)
        msgs = [Message(timestamp=1, sender_id="a", sender_name="A",
                        content="记得提交月度报表")]
        # 默认：5 >= todo(4) → pending
        default = extract_todos(msgs)
        self.assertEqual(len(default), 1)
        self.assertEqual(default[0].status, "pending")
        # 抬高 todo_threshold 到 6：5 < 6 → 降为 unconfirmed（阈值真的生效）
        unconfirmed = extract_todos(msgs, todo_threshold=6)
        self.assertEqual(unconfirmed[0].status, "unconfirmed")
        # 抬高 weak_threshold 到 6：5 < 6 → 直接被过滤
        self.assertEqual(extract_todos(msgs, weak_threshold=6), [])


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

    def test_wal_and_busy_timeout_enabled(self):
        """回归：看板/同步线程/计划任务三路并发打同一个库，
        没 WAL + busy_timeout 会随机 'database is locked'。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "t.db"))
            try:
                jm = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
                bt = store.conn.execute("PRAGMA busy_timeout").fetchone()[0]
                self.assertEqual(jm, "wal")
                self.assertGreaterEqual(int(bt), 5000)
            finally:
                store.close()

    def test_migrate_runs_once_per_schema_version(self):
        """回归：看板每 30 秒新建 Store，结构没变就跳过 _migrate 里的
        全表 UPDATE ... NOT IN 子查询，否则库大了周期性卡顿。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "t.db")
            s1 = Store(db)
            try:
                self.assertEqual(s1._meta_get("schema_version"),
                                 s1.SCHEMA_VERSION)
            finally:
                s1.close()
            # 二次打开：水位已命中，_migrate 应直接 return（不抛错即通过）
            s2 = Store(db)
            s2.close()


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
                self.assertIn('"generator": "todoseine"', json_text)
                self.assertIn('"todos"', json_text)
            finally:
                store.close()


    def test_fts_incremental_watermark(self):
        """回归：增量同步只补水位之后的消息，不再每次 NOT IN 全表 diff。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._seeded(tmp)
            try:
                first = store._meta_get("fts_last_id")
                self.assertIsNotNone(first, "首次同步后应记录 FTS 水位")
                store.add_messages([
                    Message(timestamp=999, sender_id="u9", sender_name="小李",
                            content="明天交季度总结", chat_id="g9"),
                ], session="s")
                self.assertTrue(store.search("季度总结"), "新增消息应立即可检索")
                self.assertGreater(int(store._meta_get("fts_last_id")), int(first))
                # 索引条数与消息条数对齐，说明有且仅有一次补齐、没有重复灌入
                self.assertEqual(
                    store.conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0],
                    store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
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


class TestCalibration(unittest.TestCase):
    """用户校准反馈闭环：mark_reviewed → feedback 表 → weights_map → extract 权重微调。"""

    def _store(self, tmp: str) -> Store:
        return Store(os.path.join(tmp, "chat.db"))

    def _seed_msg_and_todo(self, store: Store, chat_id: str, sender: str,
                           content: str, reason: str = "") -> int:
        """写一条 messages + todos，返回 todo_id。"""
        ts = int(datetime.now().timestamp()) - 60
        store.add_messages([
            Message(timestamp=ts, sender_id=sender, sender_name=sender,
                    content=content, chat_id=chat_id),
        ], session="qq")
        # 直接用底层 insert 跳过 extract（测试里控制 reason 更方便）
        store.conn.execute(
            "INSERT INTO todos (text, session, sender, ts, reason, confidence, status, created_at) "
            "VALUES (?, 'qq', ?, ?, ?, 0.6, 'pending', ?)",
            (content, sender, ts, reason, datetime.now().isoformat()))
        store.conn.commit()
        return store.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_review_noise_auto_records_feedback(self):
        """标为 noise → feedback 表自动记 rejected。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "chat.db"))
            try:
                tid = self._seed_msg_and_todo(
                    store, chat_id="g1", sender="张三",
                    content="@全体成员 今天放假", reason="通知全体")
                self.assertTrue(store.mark_reviewed(tid, "noise"))
                stats = store.calibration_stats()
                self.assertEqual(stats["total"], 1)
                self.assertEqual(stats.get("rejected"), 1)
                # weights 还没够 3 条样本，所以不输出
                self.assertEqual(store.load_feedback_weights(), {})
            finally:
                store.close()

    def test_weights_emerge_after_three_samples(self):
        """同 (chat_id, sender) 上连续 reject 3 次，权重表输出该 reason_tag 降权。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "chat.db"))
            try:
                for i in range(3):
                    tid = self._seed_msg_and_todo_at(
                        store, chat_id="g1", sender="张三",
                        content=f"@全体成员 放假{i}", reason="通知全体",
                        ts_offset=-60 + i)
                    store.mark_reviewed(tid, "noise")
                weights = store.load_feedback_weights()
                self.assertIn(("g1", "张三"), weights)
                self.assertIn("通知全体", weights[("g1", "张三")])
                self.assertLess(weights[("g1", "张三")]["通知全体"], 0)  # 负权重

                # 同标签在不同群不共享权重
                tid2 = self._seed_msg_and_todo_at(
                    store, chat_id="g2", sender="张三",
                    content="@全体成员 开会", reason="通知全体", ts_offset=100)
                store.mark_reviewed(tid2, "done")  # g2 里这条被确认
                weights = store.load_feedback_weights()
                # g1 里 "通知全体" 还是负，g2 里不够样本数不输出
                self.assertLess(weights[("g1", "张三")]["通知全体"], 0)
            finally:
                store.close()

    def test_extract_respects_weights(self):
        """extract_todos 接 weights_map 时，被 reject 过的 reason_tag 降分会让消息过不了阈值。"""
        # 基线：@全体成员(4) + 行动词(3) + 通知类(2) = 9 分，抽到
        msg = Message(timestamp=100, sender_id="s1", sender_name="张三",
                      content="@全体成员 放假 需要处理", chat_id="g1")
        todos = extract_todos([msg], session="qq")
        self.assertTrue(len(todos) >= 1)

        # 给 "通知全体" 强加 -0.9 权重 → 9 + (-0.9*7) = 2.7 < 3，应该被过滤
        weights = {("g1", "张三"): {"通知全体": -0.9}}
        todos = extract_todos([msg], session="qq", weights_map=weights)
        self.assertEqual(todos, [])

    def test_feedback_orphans_not_left_behind(self):
        """回归：待办删掉后校准样本必须解绑（todo_id → NULL），不留孤儿行。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "fb.db"))
            try:
                tid = self._seed_msg_and_todo(
                    store, chat_id="g1", sender="张三",
                    content="@全体成员 今天放假", reason="通知全体")
                store.mark_reviewed(tid, "noise")
                self.assertTrue(store.trash_item(tid))
                rows = store.conn.execute(
                    "SELECT todo_id FROM feedback").fetchall()
                self.assertTrue(rows, "删待办不该把校准样本一起丢掉")
                self.assertTrue(all(r[0] is None for r in rows), "孤儿行必须解绑为 NULL")
            finally:
                store.close()

    def test_calibration_roundtrip(self):
        """export_calibration → import_calibration 往返一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            store1 = Store(os.path.join(tmp, "a.db"))
            try:
                for i in range(3):
                    tid = self._seed_msg_and_todo_at(
                        store1, chat_id="g1", sender="张三",
                        content=f"@全体成员 放假{i}", reason="通知全体",
                        ts_offset=-60 + i)
                    store1.mark_reviewed(tid, "noise")
                store1.set_chat_names({"g1": "测试群"})

                dst = os.path.join(tmp, "cal.json")
                n = store1.export_calibration(dst)
                self.assertEqual(n, 3)

                store2 = Store(os.path.join(tmp, "b.db"))
                try:
                    res = store2.import_calibration(dst)
                    self.assertEqual(res["inserted"], 3)
                    self.assertEqual(res["total"]["total"], 3)
                    self.assertEqual(store2.chat_name_map(), {"g1": "测试群"})
                    self.assertEqual(store1.load_feedback_weights(),
                                     store2.load_feedback_weights())
                finally:
                    store2.close()
            finally:
                store1.close()

    def test_import_rejects_bad_schema(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "chat.db"))
            try:
                bad = os.path.join(tmp, "bad.json")
                with open(bad, "w", encoding="utf-8") as fh:
                    json.dump({"schema": "v0-legacy", "feedback": []}, fh)
                self.assertRaises(ValueError, store.import_calibration, bad)
            finally:
                store.close()

    def test_calibration_insights(self):
        """calibration_insights 返回的 active_groups / top_tags 能反映权重。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "chat.db"))
            try:
                # 先没数据
                ins = store.calibration_insights()
                self.assertEqual(ins["stats"]["total"], 0)
                self.assertEqual(ins["active_groups"], 0)
                self.assertEqual(ins["top_tags"], [])

                # 同一 (chat_id, sender) 累计 >=3 条 rejected → 权重生效
                for i in range(3):
                    tid = self._seed_msg_and_todo_at(
                        store, chat_id="g2", sender="李四",
                        content=f"记得复习章节{i}", reason="行动词:记得",
                        ts_offset=-60 - i)
                    store.mark_reviewed(tid, "noise")

                ins = store.calibration_insights()
                self.assertEqual(ins["stats"]["total"], 3)
                self.assertEqual(ins["active_groups"], 1)
                # top_tags 至少有一条，adj < 0（因为全 rejected，rate=0）
                self.assertTrue(len(ins["top_tags"]) >= 1)
                _, adj = ins["top_tags"][0]
                self.assertLess(adj, 0)
            finally:
                store.close()

    def test_explain_todo(self):
        """explain_todo 返回的 report 能正确分解规则 + 校准命中 + 原消息。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "chat.db"))
            try:
                # 空库 → None
                self.assertIsNone(store.explain_todo(999))

                # 造一条有 reason 的待办（带多 tag + 负 tag）
                tid = self._seed_msg_and_todo_at(
                    store, chat_id="g3", sender="王五",
                    content="@全体成员 明天下午三点前记得交",
                    reason="通知全体; 行动词:记得/交; 时间词:明天/三点; 指向我",
                    ts_offset=-120)

                report = store.explain_todo(tid)
                self.assertIsNotNone(report)
                self.assertEqual(report["sender"], "王五")
                self.assertEqual(report["chat_id"], "g3")
                self.assertEqual(report["threshold"], 4)

                # tag 数量：reason 分号拆分 → 4 条
                self.assertEqual(len(report["tags"]), 4)
                # 每个 tag 都有 description（tag_meta 命中）
                for t in report["tags"]:
                    self.assertTrue(t["description"], t["tag"])
                    self.assertIn("base_score", t)

                # 规则总分 + 校准 Δ = final_score（因为没权重，Δ=0，final=subtotal）
                self.assertEqual(report["subtotal"], report["final_score"])

                # 原消息（chat_id 反查得到了）
                self.assertIsNotNone(report["original_msg"])
                self.assertEqual(report["original_msg"]["chat_id"], "g3")

                # 校准权重（空的）
                self.assertEqual(report["calib_delta"], 0.0)
            finally:
                store.close()

    def _seed_msg_and_todo_at(self, store: Store, chat_id: str, sender: str,
                              content: str, reason: str, ts_offset: int = 0) -> int:
        """_seed_msg_and_todo 的变体：允许指定时间戳偏移，避免 UNIQUE 冲突。"""
        ts = int(datetime.now().timestamp()) + ts_offset
        store.add_messages([
            Message(timestamp=ts, sender_id=sender, sender_name=sender,
                    content=content, chat_id=chat_id),
        ], session="qq")
        store.conn.execute(
            "INSERT INTO todos (text, session, sender, ts, reason, confidence, status, created_at) "
            "VALUES (?, 'qq', ?, ?, ?, 0.6, 'pending', ?)",
            (content, sender, ts, reason, datetime.now().isoformat()))
        store.conn.commit()
        return store.conn.execute("SELECT last_insert_rowid()").fetchone()[0]


if __name__ == "__main__":
    unittest.main()
