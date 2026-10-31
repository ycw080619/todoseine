"""Hermes 服务测试：token 加密存储、鉴权、指令、巡检、每日汇总。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from todoseine.core.hermes import (  # noqa: E402
    HermesService, MemoryHost, daily_summary, ensure_token, inspect, load_token,
)
from todoseine.core.store import Store  # noqa: E402
from todoseine.models import TodoItem  # noqa: E402


def _cfg(**over) -> dict:
    data = {"privacy": {"enabled": True, "whitelist_groups": []},
            "ai": {"enabled": True, "base_url": "http://127.0.0.1:11434", "api_key": "k"},
            "forward": {"enabled": False}}
    data.update(over)
    return data


class TestToken(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="c2t-hermes-")

    def test_token_persisted_and_rotated(self) -> None:
        first = ensure_token(self.tmp)
        self.assertGreater(len(first), 16)
        self.assertEqual(load_token(self.tmp), first)
        self.assertEqual(ensure_token(self.tmp), first)
        rotated = ensure_token(self.tmp, rotate=True)
        self.assertNotEqual(rotated, first)
        self.assertEqual(load_token(self.tmp), rotated)

    def test_missing_token_is_empty(self) -> None:
        self.assertEqual(load_token(os.path.join(self.tmp, "nope")), "")


class TestInspect(unittest.TestCase):
    def test_clean_environment(self) -> None:
        host = MemoryHost(os.path.join(tempfile.mkdtemp(), "a.db"), _cfg())
        report = inspect(host)
        self.assertEqual(report["issues"], [])
        self.assertFalse(report["restarted"])

    def test_restart_when_not_running(self) -> None:
        host = MemoryHost(os.path.join(tempfile.mkdtemp(), "a.db"), _cfg(),
                          status_fn=lambda: {"running": False, "ai_configured": True})
        report = inspect(host)
        self.assertIn("采集未在运行", report["issues"])
        self.assertTrue(report["restarted"])

    def test_missing_ai_config_reported(self) -> None:
        host = MemoryHost(os.path.join(tempfile.mkdtemp(), "a.db"), _cfg(),
                          status_fn=lambda: {"running": True, "ai_configured": False})
        report = inspect(host)
        self.assertTrue(any("AI" in x for x in report["issues"]))


class TestSummary(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="c2t-hermes-")
        self.store = Store(os.path.join(self.tmp, "qq.db"))
        self.store.add_todos([TodoItem(text="联系 13800138000 对账", session="工作群",
                                       sender="张三", message_ts=2 ** 31 - 1,
                                       reason="关键词", confidence=0.9)])

    def tearDown(self) -> None:
        self.store.close()

    def test_summary_masks_phone(self) -> None:
        import datetime as dt

        # 把待办时间改成「今天」，让汇总能统计到
        day = dt.datetime.now()
        self.store.conn.execute("UPDATE todos SET ts=?", (int(day.timestamp()),))
        self.store.conn.commit()
        text = daily_summary(self.store, _cfg(), day)
        self.assertIn("每日汇总", text)
        self.assertNotIn("13800138000", text)
        self.assertIn("138****8000", text)


class TestService(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="c2t-hermes-")
        self.db = os.path.join(self.tmp, "qq.db")
        self.store = Store(self.db)
        self.store.add_todos([TodoItem(text="交周报", session="工作群", sender="张三",
                                       message_ts=1700000000, reason="关键词",
                                       confidence=0.9)])
        self.cfg = _cfg()
        self.host = MemoryHost(self.db, self.cfg)
        self.service = HermesService(self.host, folder=self.tmp)
        self.url = self.service.start()

    def tearDown(self) -> None:
        self.service.stop()
        self.store.close()

    def _req(self, path: str, payload=None, token: str = "") -> tuple:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.url + path, data=data, headers=headers,
                                     method="POST" if payload is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_requires_token(self) -> None:
        self.assertTrue(self.url)
        code, body = self._req("/status")
        self.assertEqual(code, 401)
        self.assertFalse(body["ok"])
        code, _ = self._req("/status", token="bad")
        self.assertEqual(code, 401)

    def test_status_and_todos(self) -> None:
        code, body = self._req("/status", token=self.service.token)
        self.assertEqual(code, 200)
        self.assertIn("running", body["status"])
        code, body = self._req("/todos", token=self.service.token)
        self.assertEqual(code, 200)
        self.assertEqual(len(body["todos"]), 1)

    def test_command_done(self) -> None:
        code, body = self._req("/command", {"command": "todos"},
                               token=self.service.token)
        self.assertEqual(code, 200)
        todo_id = body["todos"][0]["id"]
        code, body = self._req("/command", {"command": "done", "id": todo_id},
                               token=self.service.token)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.store.list_todos("pending"), [])

    def test_command_add(self) -> None:
        code, body = self._req("/command", {"command": "add", "text": "买咖啡豆"},
                               token=self.service.token)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertIn("买咖啡豆", [t.text for t in self.store.list_todos(None)])

    def test_forward_not_implemented(self) -> None:
        code, body = self._req("/command", {"command": "forward", "text": "hi"},
                               token=self.service.token)
        self.assertEqual(code, 400)
        self.assertIn("功能未启用", body["error"])

    def test_unknown_command(self) -> None:
        code, body = self._req("/command", {"command": "rm -rf /"},
                               token=self.service.token)
        self.assertEqual(code, 400)
        self.assertIn("未知指令", body["error"])


if __name__ == "__main__":
    unittest.main()
