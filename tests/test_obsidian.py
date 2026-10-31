"""Obsidian 双向同步测试：镜像格式、UUID 映射、勾选回写、冲突备份、锁。"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from todoseine.core.obsidian import ObsidianSync, acquire_lock, release_lock  # noqa: E402
from todoseine.core.store import Store  # noqa: E402
from todoseine.models import TodoItem  # noqa: E402


def _todo(text: str, ts: int = 1700000000, status: str = "pending") -> TodoItem:
    return TodoItem(text=text, session="工作群", sender="张三",
                    message_ts=ts, reason="关键词", confidence=0.9, status=status)


class TestObsidian(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="c2t-obs-")
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(self.vault)
        self.db = os.path.join(self.tmp, "qq.db")
        self.store = Store(self.db)
        self.store.add_todos([_todo("提交周报")])
        self.sync = ObsidianSync(self.vault, self.store, {})

    def tearDown(self) -> None:
        self.store.close()

    def _note(self) -> str:
        with open(self.sync.note, encoding="utf-8") as fh:
            return fh.read()

    def test_push_creates_task_line_with_uuid(self) -> None:
        self.assertTrue(self.sync.push())
        body = self._note()
        self.assertIn("- [ ] 提交周报", body)
        self.assertIn("%%c2t:", body)

    def test_push_is_incremental(self) -> None:
        self.assertTrue(self.sync.push())
        self.assertFalse(self.sync.push())       # 无变化不动磁盘

    def test_uuid_stable_across_pushes(self) -> None:
        self.sync.push()
        first = [ln for ln in self._note().splitlines() if ln.startswith("- [")][0]
        self.store.add_todos([_todo("另一件事", ts=1700000100)])
        self.sync.push()
        lines = [ln for ln in self._note().splitlines() if ln.startswith("- [")]
        self.assertIn(first, lines)

    def test_checkbox_in_obsidian_writes_back(self) -> None:
        self.sync.push()
        body = self._note().replace("- [ ] 提交周报", "- [x] 提交周报")
        with open(self.sync.note, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        result = self.sync.pull()
        self.assertEqual(result["done"], 1)
        todos = self.store.list_todos(None)
        self.assertEqual(todos[0].status, "done")

    def test_deleted_line_only_removes_mirror(self) -> None:
        self.sync.push()
        # 删掉整行 → pull 不应删除本地待办
        with open(self.sync.note, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# TodoSeine 待办\n")
        result = self.sync.pull()
        self.assertEqual(result["missing"], 1)
        self.assertEqual(len(self.store.list_todos(None)), 1)

    def test_conflict_backup_written(self) -> None:
        self.sync.push()
        # 模拟用户在 Obsidian 手改了正文（与记录的 md5 不一致）
        body = self._note().replace("提交周报", "提交周报（我改过）")
        with open(self.sync.note, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        self.store.add_todos([_todo("新增一条", ts=1700000200)])
        self.sync.push()
        backup = os.path.join(self.sync.folder, "待办.conflict.bak")
        self.assertTrue(os.path.isfile(backup))
        with open(backup, encoding="utf-8") as fh:
            self.assertIn("我改过", fh.read())

    def test_state_file_written(self) -> None:
        self.sync.push()
        self.assertTrue(os.path.isfile(self.sync.state_path))

    def test_lock_is_exclusive(self) -> None:
        self.assertTrue(acquire_lock(self.sync.folder))
        self.assertFalse(acquire_lock(self.sync.folder, wait=0.05))
        release_lock(self.sync.folder)
        self.assertTrue(acquire_lock(self.sync.folder, wait=0.05))
        release_lock(self.sync.folder)

    def test_stale_lock_taken_over(self) -> None:
        lock = os.path.join(self.sync.folder, ".c2t.lock")
        os.makedirs(self.sync.folder, exist_ok=True)
        with open(lock, "w", encoding="ascii") as fh:
            fh.write("1")
        old = time.time() - 999
        os.utime(lock, (old, old))
        self.assertTrue(acquire_lock(self.sync.folder, wait=0.2))
        release_lock(self.sync.folder)

    def test_no_vault_means_disabled(self) -> None:
        self.assertFalse(ObsidianSync("", self.store, {}).usable)


if __name__ == "__main__":
    unittest.main()
