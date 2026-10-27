"""控制台配置读写测试（不启动 GUI）。"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "tools" / "qq_db_export" / "console.py"
spec = importlib.util.spec_from_file_location("c2t_console", HERE)
console = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(console)


class TestConfigIO(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "c2t.toml"
        self.cfg.write_text(
            "db_path = 'x'\n[ai]\nenabled = false\nbase_url = 'old'\n\n[db]\nkey = 'k'\n",
            encoding="utf-8",
        )
        self._old = console.CONFIG
        console.CONFIG = self.cfg

    def tearDown(self) -> None:
        console.CONFIG = self._old
        self.tmp.cleanup()

    def test_write_keeps_other_sections(self) -> None:
        console._write_cfg({"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:14b"})
        text = self.cfg.read_text(encoding="utf-8")
        self.assertIn("base_url = 'http://127.0.0.1:11434/v1'", text)
        self.assertIn("model = 'qwen2.5:14b'", text)
        self.assertIn("enabled = false", text)
        self.assertIn("[db]", text)
        self.assertIn("key = 'k'", text)
        self.assertNotIn("base_url = 'old'", text)

    def test_read_roundtrip(self) -> None:
        console._write_cfg({"base_url": "http://localhost:11434/v1", "model": "llama3"})
        got = console._read_cfg()
        self.assertEqual(got["base_url"], "http://localhost:11434/v1")
        self.assertEqual(got["model"], "llama3")

    def test_defaults_when_missing(self) -> None:
        os.remove(self.cfg)
        got = console._read_cfg()
        self.assertIn("11434", got["base_url"])

    def test_ui_flags_read_from_config(self) -> None:
        """外观开关必须读真实配置，不能落到默认值上。"""
        self.cfg.write_text(
            "db_path = 'x'\n\n[ui]\nborder = false\nclock = false\nwidth = 420\n",
            encoding="utf-8",
        )
        self.assertEqual(console._read_ui("width"), "420")
        self.assertFalse(console._read_ui_flag("border"))
        self.assertFalse(console._read_ui_flag("clock"))

    def test_ui_flag_defaults_true(self) -> None:
        self.assertTrue(console._read_ui_flag("border"))
        self.assertTrue(console._read_ui_flag("clock"))

    def test_write_ui_bool_literal(self) -> None:
        console._write_section("ui", {"border": False, "clock": True})
        text = self.cfg.read_text(encoding="utf-8")
        self.assertIn("border = false", text)
        self.assertIn("clock = true", text)


class TestBackupRestore(unittest.TestCase):
    """一键备份 / 恢复：带时间戳复制、只留最近 5 份、恢复前自动自保。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old_root = console.ROOT
        console.ROOT = self.root

    def tearDown(self) -> None:
        console.ROOT = self._old_root
        self.tmp.cleanup()

    def _seed_db(self, content: bytes = b"live-db") -> Path:
        db = self.root / "qq.db"
        db.write_bytes(content)
        return db

    def test_backup_and_prune_keeps_five(self) -> None:
        db = self._seed_db()
        bdir = console._backups_dir()
        # 预置 6 份旧备份，加上新备份共 7 份 → 只留最近 5 份
        for i in range(6):
            (bdir / f"qq-2026102{i}-120000.db").write_bytes(b"old")
        dst = console.backup_db(db)
        self.assertTrue(dst.exists())
        self.assertEqual(dst.read_bytes(), b"live-db")
        names = [f.name for f in console._list_backups()]
        self.assertEqual(len(names), 5)
        self.assertIn(dst.name, names)
        self.assertNotIn("qq-20261020-120000.db", names)   # 最旧的被剪掉
        self.assertNotIn("qq-20261021-120000.db", names)

    def test_restore_overwrites_and_saves_current(self) -> None:
        db = self._seed_db(b"current")
        bdir = console._backups_dir()
        bak = bdir / "qq-20261026-080000.db"
        bak.write_bytes(b"backup")
        safety = console.restore_db(bak, db)
        self.assertEqual(db.read_bytes(), b"backup")        # 已覆盖成备份内容
        self.assertEqual(safety.read_bytes(), b"current")   # 原库被自保
        self.assertTrue(safety.name.startswith("qq-"))      # 也是一份正常备份（可再恢复）


if __name__ == "__main__":
    unittest.main()
