"""CLI / 配置 / 错误码相关测试。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from chat2todo import config
from chat2todo.cli import main
from chat2todo.core import Store, dedupe
from chat2todo.models import Message


def _msg(ts=1700000000, sid="u1", name="小李", content="你好"):
    return Message(timestamp=ts, sender_id=sid, sender_name=name, content=content)


class TestConfig(unittest.TestCase):
    def test_defaults_without_file(self):
        cfg = config.load("")
        self.assertEqual(cfg["db_path"], "c2t.db")
        self.assertEqual(cfg["todo_threshold"], 4)
        self.assertFalse(cfg["strict_dedupe"])
        self.assertFalse(cfg["ai"]["enabled"])
        self.assertFalse(cfg["ai"]["review"])

    def test_sample_toml_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c2t.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(config.sample_toml())
            cfg = config.load(path)
            self.assertEqual(cfg["db_path"], "c2t.db")
            self.assertEqual(cfg["export_dir"], "out")
            self.assertIn("ai", cfg)
            self.assertFalse(cfg["ai"]["enabled"])

    def test_config_with_bom(self):
        # Windows 记事本/PowerShell 写出的 TOML 常带 BOM，必须能读
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bom.toml")
            with open(path, "wb") as fh:
                fh.write('\ufeffdb_path = "x.db"\n'.encode("utf-8"))
            self.assertEqual(config.load(path)["db_path"], "x.db")


class TestStrictDedupe(unittest.TestCase):
    def test_default_merges_same_second_same_text(self):
        msgs = [_msg(), _msg()]
        self.assertEqual(len(dedupe(msgs)), 1)

    def test_strict_keeps_both(self):
        msgs = [_msg(), _msg()]
        self.assertEqual(len(dedupe(msgs, strict=True)), 2)


class TestStatsSince(unittest.TestCase):
    def test_since_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "t.db"))
            try:
                store.add_messages([_msg(ts=100), _msg(ts=1000, content="新消息")])
                self.assertEqual(store.stats()["messages"], 2)
                self.assertEqual(store.stats(since_ts=500)["messages"], 1)
            finally:
                store.close()


class TestCli(unittest.TestCase):
    def test_doctor_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                code = main(["doctor", "--db", os.path.join(tmp, "c2t.db")])
            finally:
                os.chdir(cwd)
            self.assertIn(code, (0, 1))

    def test_import_missing_file_returns_e1001(self):
        # main() 会把 C2TError 渲染成错误提示并返回 1，而不是把异常抛给用户
        code = main(["import", "does_not_exist.txt"])
        self.assertEqual(code, 1)

    def test_since_parse(self):
        from chat2todo.cli import _parse_since

        self.assertGreater(_parse_since("7d"), 0)
        self.assertGreater(_parse_since("24h"), 0)


if __name__ == "__main__":
    unittest.main()
