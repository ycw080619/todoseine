"""桌面美化联动测试：脱敏输出、ICS、防抖、本地接口鉴权与回写。"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from todoseine.core.desktop_link import (  # noqa: E402
    DesktopLink, build_ics, ensure_token, export_items, load_token,
)
from todoseine.core.store import Store  # noqa: E402
from todoseine.models import TodoItem  # noqa: E402


def _todo(text: str, ts: int = 1700000000, status: str = "pending") -> TodoItem:
    return TodoItem(text=text, session="工作群", sender="张三",
                    message_ts=ts, reason="关键词", confidence=0.9, status=status)


def _cfg(enabled: bool = True, **desktop) -> dict:
    data = {"enabled": enabled, "json": True, "ics": True, "port": 0,
            "debounce_ms": 1500}
    data.update(desktop)
    return {"privacy": {"enabled": True, "whitelist_groups": []}, "desktop": data}


class TestDesktopLink(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="c2t-deck-")
        self.db = os.path.join(self.tmp, "qq.db")
        self.out = os.path.join(self.tmp, "desktop")
        self.store = Store(self.db)
        self.store.add_todos([_todo("明天下午三点前提交周报"),
                              _todo("联系 13800138000 对账", ts=1700000100)])
        self.cfg = _cfg(dir=self.out)
        self.link = DesktopLink(self.cfg, self.db)

    def tearDown(self) -> None:
        self.link.stop_server()
        self.store.close()

    # ---------- 输出 ----------

    def test_export_writes_redacted_json(self) -> None:
        self.assertTrue(self.link.export())
        with open(os.path.join(self.out, "todos.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["count"], 2)
        texts = " ".join(item["text"] for item in data["todos"])
        self.assertNotIn("13800138000", texts)
        self.assertIn("138****8000", texts)

    def test_export_skips_when_unchanged(self) -> None:
        self.assertTrue(self.link.export())
        self.assertFalse(self.link.export())

    def test_whitelisted_group_keeps_original(self) -> None:
        cfg = _cfg(dir=self.out)
        cfg["privacy"]["whitelist_groups"] = ["工作群"]
        items = export_items(self.store, cfg)
        self.assertTrue(any("13800138000" in i["text"] for i in items))

    def test_disabled_writes_nothing(self) -> None:
        link = DesktopLink(_cfg(enabled=False, dir=self.out), self.db)
        self.assertFalse(link.export())
        self.assertFalse(os.path.isdir(self.out))

    def test_ics_has_event_and_escape(self) -> None:
        items = export_items(self.store, self.cfg)
        ics = build_ics([{
            "id": 7, "text": "交周报, 并;抄送", "session": "工作群",
            "sender": "张三", "status": "pending", "due": 1750000000,
        }])
        self.assertIn("BEGIN:VCALENDAR", ics)
        self.assertIn("UID:c2t-7@todoseine.local", ics)
        self.assertIn("DTSTART;VALUE=DATE:", ics)
        self.assertIn("交周报\\, 并\\;抄送", ics)
        self.assertIn("STATUS:NEEDS-ACTION", ics)
        self.assertEqual(items[0]["due"] > 0, True)   # 「明天下午三点前」能解析出截止日

    def test_json_due_date_without_deadline_is_zero(self) -> None:
        items = export_items(self.store, self.cfg)
        self.assertEqual([i for i in items if "对账" in i["text"]][0]["due"], 0)

    # ---------- 防抖 ----------

    def test_debounce_only_writes_once(self) -> None:
        self.link.schedule(0.15)
        self.link.schedule(0.15)
        deadline = time.time() + 3
        while time.time() < deadline and not os.path.isfile(
                os.path.join(self.out, "todos.json")):
            time.sleep(0.05)
        self.assertTrue(os.path.isfile(os.path.join(self.out, "todos.json")))
        # 再调一次 schedule 后内容没变 → 不应重写（md5 未变，文件 mtime 不变）
        mtime = os.path.getmtime(os.path.join(self.out, "todos.json"))
        time.sleep(0.2)
        self.link.schedule(0.1)
        time.sleep(0.4)
        self.assertEqual(os.path.getmtime(os.path.join(self.out, "todos.json")), mtime)

    # ---------- token ----------

    def test_token_persisted_and_rotated(self) -> None:
        first = ensure_token(self.out)
        self.assertGreater(len(first), 16)
        self.assertEqual(load_token(self.out), first)
        self.assertEqual(ensure_token(self.out), first)
        rotated = ensure_token(self.out, rotate=True)
        self.assertNotEqual(rotated, first)
        self.assertEqual(load_token(self.out), rotated)
        again = self.link.rotate_token()
        self.assertNotEqual(again, rotated)
        self.assertEqual(load_token(self.out), again)

    # ---------- 本地服务 ----------

    def _start(self) -> str:
        url = self.link.start_server()
        self.assertTrue(url, "服务未启动")
        with open(os.path.join(self.out, "endpoint.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertEqual(meta["url"], url)
        return url

    def _get(self, url: str, path: str, token: str = "") -> tuple:
        req = urllib.request.Request(url + path)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, url: str, path: str, payload: dict, token: str = "") -> tuple:
        raw = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url + path, data=raw, method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_server_rejects_bad_token(self) -> None:
        url = self._start()
        code, body = self._get(url, "/todos")
        self.assertEqual(code, 401)
        self.assertFalse(body["ok"])
        code, _ = self._get(url, "/todos", token="wrong-token")
        self.assertEqual(code, 401)

    def test_server_rejects_foreign_host(self) -> None:
        url = self._start()
        port = int(url.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/todos", headers={
            "Host": "evil.example.com", "Authorization": f"Bearer {self.link.token}"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 403)
        conn.close()

    def test_server_done_callback_updates_db(self) -> None:
        url = self._start()
        code, body = self._get(url, "/todos", token=self.link.token)
        self.assertEqual(code, 200)
        self.assertEqual(body["count"], 2)
        todo_id = body["todos"][0]["id"]
        code, body = self._post(url, "/done", {"id": todo_id}, token=self.link.token)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.store.list_todos("pending")[0].text.startswith("联系"), True)

    def test_server_edit_and_delete(self) -> None:
        url = self._start()
        code, body = self._get(url, "/todos", token=self.link.token)
        todo_id = body["todos"][0]["id"]
        code, _ = self._post(url, "/edit", {"id": todo_id, "text": "改过的内容"},
                             token=self.link.token)
        self.assertEqual(code, 200)
        self.assertIn("改过的内容", [t.text for t in self.store.list_todos(None)])
        code, _ = self._post(url, "/delete", {"id": todo_id}, token=self.link.token)
        self.assertEqual(code, 200)
        self.assertNotIn("改过的内容", [t.text for t in self.store.list_todos(None)])

    def test_server_rejects_bad_body(self) -> None:
        url = self._start()
        code, _ = self._post(url, "/done", {"id": "abc"}, token=self.link.token)
        self.assertEqual(code, 400)
        code, _ = self._post(url, "/done", {"id": 999999}, token=self.link.token)
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
