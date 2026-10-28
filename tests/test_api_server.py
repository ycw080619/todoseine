"""本地 HTTP API：端点返回 JSON 正确、边界拦截生效。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import quote

from chat2todo.api import create_server, _cors_origin, _is_local
from chat2todo.core.store import Store
from chat2todo.models import Message, TodoItem


class TestApiServer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "api.db")
        store = Store(self.db)
        try:
            store.add_messages([
                Message(timestamp=100, sender_id="u1", sender_name="张三",
                        content="明天记得交作业", chat_id="g1"),
            ])
            store.add_todos([
                TodoItem(text="明天记得交作业", session="g1", sender="张三",
                         message_ts=100, reason="行动词:记得", confidence=0.8),
            ])
        finally:
            store.close()

        self.server = create_server(self.db, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    # --- 工具 ---------------------------------------------------
    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, path, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else b""
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    # --- 端点 ---------------------------------------------------
    def test_health(self):
        code, body = self._get("/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "chat2todo")

    def test_todos(self):
        code, body = self._get("/todos")
        self.assertEqual(code, 200)
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["todos"][0]["text"], "明天记得交作业")

    def test_todos_filter_status(self):
        code, body = self._get("/todos?status=done")
        self.assertEqual(code, 200)
        self.assertEqual(body["count"], 0)

    def test_todos_bad_status(self):
        code, body = self._get("/todos?status=whatever")
        self.assertEqual(code, 400)
        self.assertIn("status", body["error"])

    def test_stats(self):
        code, body = self._get("/stats")
        self.assertEqual(code, 200)
        self.assertIn("stats", body)
        self.assertIn("weekly", body)

    def test_search(self):
        code, body = self._get("/search?q=" + quote("作业"))
        self.assertEqual(code, 200)
        self.assertGreaterEqual(body["count"], 1)

    def test_search_missing_q(self):
        code, body = self._get("/search")
        self.assertEqual(code, 400)

    def test_mark_done(self):
        code, body = self._post("/todos/1/done")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "done")
        # 复查：已不在 pending 里
        _, listed = self._get("/todos?status=pending")
        self.assertEqual(listed["count"], 0)

    def test_mark_done_missing_id(self):
        code, body = self._post("/todos/9999/done")
        self.assertEqual(code, 404)

    def test_unknown_endpoint(self):
        code, _ = self._get("/nope")
        self.assertEqual(code, 404)

    # --- 边界 ---------------------------------------------------
    def test_is_local_helper(self):
        self.assertTrue(_is_local("127.0.0.1"))
        self.assertTrue(_is_local("::1"))
        self.assertTrue(_is_local("127.0.1.5"))
        self.assertFalse(_is_local("8.8.8.8"))
        self.assertFalse(_is_local("192.168.1.9"))

    def test_is_local_rejects_prefix_lookalike_hosts(self):
        """回归：startswith("127.") 会把恶意域名当回环地址，放行 CORS。"""
        for host in ("127.0.0.1.evil.com", "127.0.0.1.nip.io",
                     "localhost.evil.com", "127.0.0.1evil.com",
                     "127.evil.com", ""):
            self.assertFalse(_is_local(host), host)

    def test_cors_only_localhost(self):
        self.assertEqual(_cors_origin("http://localhost:3000"), "http://localhost:3000")
        self.assertEqual(_cors_origin("http://127.0.0.1:5173"), "http://127.0.0.1:5173")
        self.assertIsNone(_cors_origin("http://evil.example.com"))
        self.assertIsNone(_cors_origin(""))

    def test_cors_header_on_local_origin(self):
        url = f"http://127.0.0.1:{self.port}/health"
        req = urllib.request.Request(url, headers={"Origin": "http://localhost:3000"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"),
                             "http://localhost:3000")

    def test_no_cors_header_on_foreign_origin(self):
        url = f"http://127.0.0.1:{self.port}/health"
        req = urllib.request.Request(url, headers={"Origin": "http://evil.example.com"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))
