"""备份与维护测试：DPAPI 可降级、每日去重、一键清空范围、checkpoint。"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from todoseine.core import backup, maintenance  # noqa: E402


def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('hello')")
        conn.commit()
    finally:
        conn.close()


class TestBackup(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="c2t-backup-")
        self.db = os.path.join(self.tmp, "qq.db")
        _make_db(self.db)

    def test_backup_creates_file(self) -> None:
        path = backup.backup_database(self.db, os.path.join(self.tmp, "b"),
                                      keep=3, encrypt=False)
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as fh:
            self.assertTrue(fh.read().startswith(b"SQLite format"))

    def test_keep_prunes_old(self) -> None:
        target = os.path.join(self.tmp, "b")
        os.makedirs(target)
        for i in range(1, 6):
            with open(os.path.join(target, f"qq-2000010{i}-000000.db"), "wb") as fh:
                fh.write(b"x")
        backup._prune(target, 2)          # noqa: SLF001 - 直接验证保留策略
        self.assertEqual(
            sorted(os.listdir(target)), ["qq-20000104-000000.db", "qq-20000105-000000.db"])

    def test_keep_zero_keeps_all(self) -> None:
        target = os.path.join(self.tmp, "bz")
        os.makedirs(target)
        with open(os.path.join(target, "qq-20000101-000000.db"), "wb") as fh:
            fh.write(b"x")
        backup._prune(target, 0)          # noqa: SLF001
        self.assertEqual(len(os.listdir(target)), 1)

    def test_daily_only_once(self) -> None:
        cfg = {"db_path": self.db,
               "backup": {"enabled": True, "dir": os.path.join(self.tmp, "d"),
                          "keep": 3, "encrypt": False}}
        first = backup.daily_backup(cfg)
        self.assertIsNotNone(first)
        self.assertIsNone(backup.daily_backup(cfg))

    def test_backup_disabled(self) -> None:
        cfg = {"db_path": self.db, "backup": {"enabled": False}}
        self.assertIsNone(backup.daily_backup(cfg))

    def test_missing_db_returns_none(self) -> None:
        self.assertIsNone(backup.backup_database(os.path.join(self.tmp, "nope.db")))

    @unittest.skipUnless(os.name == "nt", "DPAPI 仅 Windows")
    def test_dpapi_roundtrip(self) -> None:
        raw = b"secret-bytes"
        enc = backup.dpapi_protect(raw)
        if enc is None:      # 某些受限环境拿不到 DPAPI
            self.skipTest("DPAPI 不可用")
        self.assertEqual(backup.dpapi_unprotect(enc), raw)

    def test_restore_plain(self) -> None:
        target = os.path.join(self.tmp, "b2")
        path = backup.backup_database(self.db, target, encrypt=False)
        db2 = os.path.join(self.tmp, "restored.db")
        self.assertTrue(backup.restore_backup(path, db2))
        conn = sqlite3.connect(db2)
        try:
            self.assertEqual(conn.execute("SELECT v FROM t").fetchone()[0], "hello")
        finally:
            conn.close()


class TestMaintenance(unittest.TestCase):
    def test_checkpoint_vacuum_on_missing_file_is_noop(self) -> None:
        maintenance.checkpoint_and_vacuum("")          # 不应抛异常

    def test_wipe_removes_archive_and_db(self) -> None:
        tmp = tempfile.mkdtemp(prefix="c2t-wipe-")
        db = os.path.join(tmp, "qq.db")
        _make_db(db)
        archive = os.path.join(tmp, "archive")
        os.makedirs(archive)
        with open(os.path.join(archive, "x.md"), "w", encoding="utf-8") as fh:
            fh.write("data")
        cfg = {"db_path": db, "archive": {"dir": archive},
               "backup": {"dir": os.path.join(tmp, "backups")}}
        home = os.path.join(tmp, "home")
        os.makedirs(os.path.join(home, "plugins"))   # 插件本体必须保留
        with open(os.path.join(home, "app.log"), "w", encoding="utf-8") as fh:
            fh.write("log")
        removed = maintenance.wipe_all(cfg, home_dir=home)
        self.assertFalse(os.path.exists(db))
        self.assertFalse(os.path.isdir(archive))
        self.assertTrue(removed)
        # 插件本体保留、日志被清掉
        self.assertTrue(os.path.isdir(os.path.join(home, "plugins")))
        self.assertFalse(os.path.exists(os.path.join(home, "app.log")))


if __name__ == "__main__":
    unittest.main()
