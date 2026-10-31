"""schema 迁移测试：版本对比 / 成功迁移 / 失败不碰正式库 / 备份落盘。"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from todoseine.core import migrate  # noqa: E402
from todoseine.core.store import Store  # noqa: E402


def _old_db(path: str, version: str = "5") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (version,))
        conn.execute(
            "CREATE TABLE todos (id INTEGER PRIMARY KEY, text TEXT, session TEXT,"
            " sender TEXT, ts INTEGER, reason TEXT, confidence REAL, status TEXT)")
        conn.execute(
            "INSERT INTO todos (text, session, sender, ts, reason, confidence, status)"
            " VALUES ('旧库待办', '工作群', '张三', 1700000000, '关键词', 0.9, 'pending')")
        conn.commit()
    finally:
        conn.close()


class TestMigrate(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="c2t-migrate-")
        self.db = os.path.join(self.tmp, "qq.db")

    def test_fresh_db_needs_no_migration(self) -> None:
        store = Store(self.db)
        store.close()
        info = migrate.plan(self.db)
        self.assertFalse(info["needed"])
        self.assertEqual(info["current"], info["target"])

    def test_missing_db(self) -> None:
        info = migrate.plan(os.path.join(self.tmp, "none.db"))
        self.assertFalse(info["exists"])
        self.assertTrue(info["needed"])
        result = migrate.run(os.path.join(self.tmp, "none.db"))
        self.assertFalse(result["ok"])

    def test_old_db_migrated_with_backup(self) -> None:
        _old_db(self.db)
        info = migrate.plan(self.db)
        self.assertTrue(info["needed"])
        result = migrate.run(self.db)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(result["backup"])
        self.assertTrue(os.path.isfile(result["backup"]))
        self.assertEqual(migrate._read_version(self.db), Store.SCHEMA_VERSION)
        store = Store(self.db)
        try:
            cols = {r[1] for r in store.conn.execute("PRAGMA table_info(todos)")}
            self.assertIn("reviewed", cols)
            self.assertIn("done_at", cols)
            texts = [t.text for t in store.list_todos(None)]
            self.assertIn("旧库待办", texts)
        finally:
            store.close()

    def test_failure_keeps_original_db(self) -> None:
        _old_db(self.db)
        with mock.patch.object(Store, "_migrate", side_effect=RuntimeError("boom")):
            result = migrate.run(self.db)
        self.assertFalse(result["ok"])
        self.assertIn("boom", result["error"])
        # 正式库仍是老版本，数据一条不少
        self.assertEqual(migrate._read_version(self.db), "5")
        conn = sqlite3.connect(self.db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_second_run_is_noop(self) -> None:
        _old_db(self.db)
        self.assertTrue(migrate.run(self.db)["ok"])
        result = migrate.run(self.db)
        self.assertTrue(result["ok"])
        self.assertEqual(result["from"], result["to"])
        self.assertEqual(result["backup"], "")


if __name__ == "__main__":
    unittest.main()
