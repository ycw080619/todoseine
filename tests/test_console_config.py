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


if __name__ == "__main__":
    unittest.main()
