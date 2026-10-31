"""插件宿主测试：扫描 / 权限白名单 / 子进程隔离 / 崩溃计数 / 安全模式 / 回滚。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from todoseine.core.plugins import (  # noqa: E402
    CRASH_WINDOW, PluginError, PluginManager, crash_count,
)

_GOOD = {
    "schema": "chat2todo-plugin-v1",
    "id": "hello",
    "name": "示例插件",
    "version": "0.1.0",
    "permission_level": "file",
    "entry": "entry.py",
    "permissions": ["messages.read"],
}

_OK_SCRIPT = """
import json, os
event = json.loads(os.environ["C2T_EVENT"])
print(json.dumps({"echo": event["event"], "n": len(event["payload"])}, ensure_ascii=False))
"""

_FAIL_SCRIPT = "raise SystemExit(3)\n"


class TestScan(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.mkdtemp(prefix="c2t-plugins-")
        self.mgr = PluginManager(folder=self.folder, timeout=5)

    def _install(self, pid: str = "hello", manifest: dict = None,
                 script: str = _OK_SCRIPT) -> str:
        path = os.path.join(self.folder, pid)
        os.makedirs(path, exist_ok=True)
        data = dict(_GOOD) if manifest is None else manifest
        with open(os.path.join(path, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        with open(os.path.join(path, "entry.py"), "w", encoding="utf-8") as fh:
            fh.write(script)
        return path

    def test_scan_valid_manifest(self) -> None:
        self._install()
        plugins = self.mgr.scan()
        self.assertEqual([p.id for p in plugins], ["hello"])
        self.assertEqual(plugins[0].permissions, ["messages.read"])

    def test_scan_skips_invalid_manifest(self) -> None:
        self._install("broken", manifest={"schema": "nope", "id": "x"})
        self.assertEqual(self.mgr.scan(), [])

    def test_enable_creates_snapshot(self) -> None:
        self._install()
        result = self.mgr.set_enabled("hello", True)
        self.assertTrue(result["snapshot"])
        self.assertTrue(os.path.isfile(result["snapshot"]))
        self.assertTrue(self.mgr.is_enabled("hello"))

    def test_disable_without_snapshot(self) -> None:
        self._install()
        self.mgr.set_enabled("hello", True)
        result = self.mgr.set_enabled("hello", False)
        self.assertEqual(result["snapshot"], "")
        self.assertFalse(self.mgr.is_enabled("hello"))

    def test_enable_unknown_plugin(self) -> None:
        with self.assertRaises(PluginError):
            self.mgr.set_enabled("nobody", True)

    # ---------- 子进程调用 ----------

    def test_invoke_returns_result(self) -> None:
        self._install()
        self.mgr.set_enabled("hello", True)
        out = self.mgr.invoke("hello", "message.new", {"a": 1, "b": 2})
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"], {"echo": "message.new", "n": 2})

    def test_invoke_rejects_undeclared_permission(self) -> None:
        self._install()
        self.mgr.set_enabled("hello", True)
        with self.assertRaises(PluginError):
            self.mgr.invoke("hello", "notify", {})

    def test_invoke_disabled_plugin(self) -> None:
        self._install()
        with self.assertRaises(PluginError):
            self.mgr.invoke("hello", "message.new", {})

    def test_invoke_crash_records_and_safe_mode(self) -> None:
        self._install(script=_FAIL_SCRIPT)
        self.mgr.set_enabled("hello", True)
        first = self.mgr.invoke("hello", "message.new", {})
        self.assertFalse(first["ok"])
        self.assertFalse(first["safe_mode"])
        self.assertTrue(self.mgr.is_enabled("hello"))

        second = self.mgr.invoke("hello", "message.new", {})
        self.assertTrue(second["ok"] is False)
        self.assertTrue(self.mgr.safe_mode)
        self.assertFalse(self.mgr.is_enabled("hello"))

    def test_safe_mode_blocks_enable(self) -> None:
        self._install(script=_FAIL_SCRIPT)
        self.mgr.set_enabled("hello", True)
        self.mgr.invoke("hello", "message.new", {})
        self.mgr.invoke("hello", "message.new", {})
        with self.assertRaises(PluginError):
            self.mgr.set_enabled("hello", True)

    def test_exit_safe_mode_restores(self) -> None:
        self._install(script=_FAIL_SCRIPT)
        self.mgr.set_enabled("hello", True)
        self.mgr.invoke("hello", "message.new", {})
        self.mgr.invoke("hello", "message.new", {})
        restored = self.mgr.exit_safe_mode(restore=True)
        self.assertEqual(restored, ["hello"])
        self.assertFalse(self.mgr.safe_mode)
        self.assertTrue(self.mgr.is_enabled("hello"))

    def test_invoke_blocks_in_safe_mode(self) -> None:
        self._install()
        self.mgr.set_enabled("hello", True)
        self.mgr.enter_safe_mode("测试")
        with self.assertRaises(PluginError):
            self.mgr.invoke("hello", "message.new", {})

    # ---------- 崩溃窗口 ----------

    def test_crash_window_expires(self) -> None:
        state = {"crashes": {"hello": [
            (datetime.now() - CRASH_WINDOW - timedelta(days=1)).isoformat(timespec="seconds"),
            (datetime.now() - CRASH_WINDOW - timedelta(days=2)).isoformat(timespec="seconds"),
        ]}}
        self.assertEqual(crash_count(state, "hello"), 0)

    # ---------- 回滚 ----------

    def test_rollback_restores_plugin_folder(self) -> None:
        install = self._install()
        self.mgr.set_enabled("hello", True)          # 触发快照
        target = os.path.join(install, "entry.py")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("# 被改坏了\n")
        snapshot = self.mgr.rollback()
        self.assertTrue(snapshot)
        with open(target, encoding="utf-8") as fh:
            self.assertIn("C2T_EVENT", fh.read())

    def test_rollback_without_snapshot(self) -> None:
        self.assertIsNone(self.mgr.rollback())

    def test_rollback_clears_safe_mode(self) -> None:
        self._install()
        self.mgr.set_enabled("hello", True)
        self.mgr.enter_safe_mode("测试")
        self.assertIsNotNone(self.mgr.rollback())
        self.assertFalse(self.mgr.safe_mode)


if __name__ == "__main__":
    unittest.main()
