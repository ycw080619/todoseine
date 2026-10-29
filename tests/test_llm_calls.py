"""LLM 调用可观测性测试：on_call 回调 + store 落库/统计。"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from chat2todo.ai import AI  # noqa: E402
from chat2todo.core import Store  # noqa: E402


def _fake_resp(body: dict):
    raw = json.dumps(body).encode("utf-8")
    resp = io.BytesIO(raw)
    resp.headers = {}
    return resp


class TestOnCallCallback(unittest.TestCase):
    def _make_ai(self):
        # base_url 必须过出口白名单：明文 http 只允许本机/内网
        cfg = {"ai": {"enabled": True, "base_url": "http://127.0.0.1:1",
                      "api_key": "k", "review": True}}
        records = []
        ai = AI(cfg, on_call=records.append)
        return ai, records

    def test_success_records_tokens_and_latency(self):
        ai, records = self._make_ai()
        body = {
            "choices": [{"message": {"content": "是"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }
        with patch("chat2todo.ai._NO_REDIRECT_OPENER.open", return_value=_fake_resp(body)):
            ai.review("记得提交")
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["feature"], "review")
        self.assertEqual(r["in_tokens"], 12)
        self.assertEqual(r["out_tokens"], 3)
        self.assertEqual(r["failed"], 0)
        self.assertGreaterEqual(r["latency_ms"], 0)

    def test_failure_records_failed_flag(self):
        ai, records = self._make_ai()
        with patch("chat2todo.ai._NO_REDIRECT_OPENER.open", side_effect=ConnectionError("boom")):
            result = ai.review("记得提交")
        self.assertIsNone(result)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["failed"], 1)
        self.assertEqual(records[0]["in_tokens"], 0)

    def test_callback_exception_swallowed(self):
        """回调抛异常不能影响主流程（AI 失败仍退规则）。"""
        def bad_cb(_):
            raise RuntimeError("callback dead")

        cfg = {"ai": {"enabled": True, "base_url": "http://127.0.0.1:1",
                      "api_key": "k", "review": True}}
        ai = AI(cfg, on_call=bad_cb)
        body = {"choices": [{"message": {"content": "是"}}], "usage": {}}
        with patch("chat2todo.ai._NO_REDIRECT_OPENER.open", return_value=_fake_resp(body)):
            result = ai.review("记得提交")
        self.assertTrue(result)  # 不抛异常，正常返回

    def test_no_callback_no_crash(self):
        cfg = {"ai": {"enabled": True, "base_url": "http://127.0.0.1:1",
                      "api_key": "k", "review": True}}
        ai = AI(cfg)  # 无 on_call
        body = {"choices": [{"message": {"content": "否"}}], "usage": {}}
        with patch("chat2todo.ai._NO_REDIRECT_OPENER.open", return_value=_fake_resp(body)):
            result = ai.review("记得提交")
        self.assertFalse(result)


class TestStoreLLMCalls(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()
        os.unlink(self.db)

    def test_add_and_stats(self):
        rows = [
            {"feature": "review", "model": "gpt-4o-mini", "in_tokens": 10,
             "out_tokens": 2, "latency_ms": 100, "failed": 0},
            {"feature": "review", "model": "gpt-4o-mini", "in_tokens": 20,
             "out_tokens": 4, "latency_ms": 300, "failed": 1},
            {"feature": "rewrite", "model": "gpt-4o-mini", "in_tokens": 50,
             "out_tokens": 30, "latency_ms": 500, "failed": 0},
        ]
        n = self.store.add_llm_calls(rows)
        self.assertEqual(n, 3)
        stats = self.store.llm_stats()
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["features"]["review"]["calls"], 2)
        self.assertEqual(stats["features"]["review"]["failed"], 1)
        self.assertEqual(stats["features"]["rewrite"]["calls"], 1)


if __name__ == "__main__":
    unittest.main()
