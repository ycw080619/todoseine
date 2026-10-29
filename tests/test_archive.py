"""消息 Markdown 归档测试：目录结构、manifest 去重、脱敏、白名单。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from chat2todo.core.archive import Archiver, default_archive_dir  # noqa: E402
from chat2todo.models import Message  # noqa: E402


def _msg(ts: int, sender: str = "张三", content: str = "明天交报告") -> Message:
    return Message(timestamp=ts, sender_id="u1", sender_name=sender, content=content)


class TestArchive(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="c2t-arch-")
        self.cfg = {"archive": {"enabled": True, "dir": self.tmp},
                    "privacy": {"enabled": True, "mask_logs": True,
                                "board_show_original": False,
                                "whitelist_groups": [], "custom_rules": []}}

    def _files(self):
        out = []
        for root, _dirs, names in os.walk(self.tmp):
            for name in names:
                out.append(os.path.join(root, name))
        return out

    def test_layout_and_manifest(self) -> None:
        ts = 1700000000       # 2023-11-15 06:13:20 +0800
        n = Archiver(self.cfg).write([_msg(ts)], session="工作群")
        self.assertEqual(n, 1)
        expected = os.path.join(self.tmp, "工作群", "2023", "11", "2023-11-15.md")
        self.assertTrue(os.path.isfile(expected))
        with open(expected, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("工作群 · 2023-11-15", body)
        self.assertIn("**张三**", body)
        manifest = json.load(open(os.path.join(self.tmp, "manifest.json"), encoding="utf-8"))
        self.assertIn("工作群/2023/11/2023-11-15.md", manifest["files"])

    def test_second_write_is_deduped(self) -> None:
        msgs = [_msg(1700000000)]
        self.assertEqual(Archiver(self.cfg).write(msgs, session="工作群"), 1)
        self.assertEqual(Archiver(self.cfg).write(msgs, session="工作群"), 0)
        path = os.path.join(self.tmp, "工作群", "2023", "11", "2023-11-15.md")
        self.assertEqual(open(path, encoding="utf-8").read().count("明天交报告"), 1)

    def test_same_text_in_two_groups_both_archived(self) -> None:
        msgs = [_msg(1700000000)]
        self.assertEqual(Archiver(self.cfg).write(msgs, session="群A"), 1)
        self.assertEqual(Archiver(self.cfg).write(msgs, session="群B"), 1)

    def test_phone_masked(self) -> None:
        Archiver(self.cfg).write([_msg(1700000000, content="电话 13812345678")],
                                 session="工作群")
        path = os.path.join(self.tmp, "工作群", "2023", "11", "2023-11-15.md")
        body = open(path, encoding="utf-8").read()
        self.assertIn("138****5678", body)
        self.assertNotIn("13812345678", body)

    def test_whitelist_group_keeps_original(self) -> None:
        self.cfg["privacy"]["whitelist_groups"] = ["家人群"]
        Archiver(self.cfg).write([_msg(1700000000, content="电话 13812345678")],
                                 session="家人群")
        path = os.path.join(self.tmp, "家人群", "2023", "11", "2023-11-15.md")
        self.assertIn("13812345678", open(path, encoding="utf-8").read())

    def test_disabled_writes_nothing(self) -> None:
        self.cfg["archive"]["enabled"] = False
        self.assertEqual(Archiver(self.cfg).write([_msg(1700000000)], session="群"), 0)
        self.assertEqual(self._files(), [])

    def test_unsafe_group_name_sanitized(self) -> None:
        Archiver(self.cfg).write([_msg(1700000000)], session='a/b:c*?"<>|')
        dirs = [n for n in os.listdir(self.tmp) if os.path.isdir(os.path.join(self.tmp, n))]
        self.assertEqual(dirs, ["a_b_c______"])

    def test_default_dir_next_to_db(self) -> None:
        self.assertTrue(default_archive_dir(r"C:\data\qq.db").endswith("archive"))
        self.assertEqual(default_archive_dir(""), "")


if __name__ == "__main__":
    unittest.main()
