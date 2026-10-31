"""半自动转发的核心逻辑：审计、哈希、内容规整、平台守卫。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from todoseine.core import forward


class TestForwardCore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "forward_audit.jsonl")
        self._patch = mock.patch.object(forward, "audit_path", lambda: self.path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def test_disabled_by_default(self) -> None:
        """文档要求默认关闭：空配置与缺段都必须判定为关闭。"""
        self.assertFalse(forward.enabled({}))
        self.assertFalse(forward.enabled({"forward": {}}))
        self.assertTrue(forward.enabled({"forward": {"enabled": True}}))

    def test_prepare_strips_and_truncates(self) -> None:
        self.assertEqual(forward.prepare("  你好  "), "你好")
        long = "字" * 3000
        self.assertEqual(len(forward.prepare(long)), 2000)

    def test_content_hash_is_short_and_stable(self) -> None:
        h1 = forward.content_hash("明天下午交报告")
        h2 = forward.content_hash("明天下午交报告")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)
        self.assertNotEqual(h1, forward.content_hash("别的话"))

    def test_audit_never_records_raw_text(self) -> None:
        """审计只留目标、哈希、长度与结果，原文绝不落盘。"""
        secret = "张三明天下午三点把合同送到 3 楼"
        forward.record("某群", secret, "sent")
        with open(self.path, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("张三", raw)
        entry = json.loads(raw.strip().splitlines()[0])
        self.assertEqual(entry["target"], "某群")
        self.assertEqual(entry["result"], "sent")
        self.assertEqual(entry["hash"], forward.content_hash(secret))
        self.assertEqual(entry["chars"], len(secret))

    def test_read_audit_returns_recent(self) -> None:
        for i in range(3):
            forward.record("群A", f"第{i}条", "sent")
        rows = forward.read_audit(limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["hash"], forward.content_hash("第2条"))

    def test_read_audit_missing_file(self) -> None:
        self.assertEqual(forward.read_audit(), [])

    def test_simulate_send_rejects_empty(self) -> None:
        """空内容绝不注入按键。"""
        self.assertFalse(forward.simulate_send("   "))


if __name__ == "__main__":
    unittest.main()
