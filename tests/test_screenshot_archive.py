"""截图归档（可选）的落盘与保留期清理。"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from todoseine.core import screenshot_archive as sa


def _cfg(root: str, enabled: bool = True, days: int = 7) -> dict:
    return {"db_path": os.path.join(root, "qq.db"),
            "archive": {"dir": os.path.join(root, "archive"),
                        "screenshots": enabled,
                        "screenshot_days": days}}


class TestScreenshotArchive(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_disabled_by_default_writes_nothing(self) -> None:
        """默认关闭时绝不落盘（OCR 临时截图只走内存）。"""
        cfg = _cfg(self.root, enabled=False)
        self.assertEqual(sa.screenshot_dir(cfg), "")
        self.assertEqual(sa.save(b"\x89PNG", cfg), "")

    def test_save_writes_into_separate_dir(self) -> None:
        cfg = _cfg(self.root)
        path = sa.save(b"\x89PNG-fake", cfg, ts=1_700_000_000)
        self.assertTrue(path.endswith(".png"))
        self.assertIn(os.sep + "screenshots" + os.sep, path)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.listdir(os.path.dirname(path)), [os.path.basename(path)])

    def test_purge_removes_only_expired(self) -> None:
        cfg = _cfg(self.root, days=7)
        now = int(time.time())
        old = sa.save(b"old", cfg, ts=now - 10 * 86400)
        fresh = sa.save(b"new", cfg, ts=now)
        self.assertTrue(os.path.isfile(old) and os.path.isfile(fresh))
        dropped = sa.purge(cfg, now=now)
        self.assertEqual(dropped, 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_purge_noop_when_disabled(self) -> None:
        cfg = _cfg(self.root, enabled=False, days=1)
        folder = os.path.join(self.root, "archive", "screenshots")
        os.makedirs(folder, exist_ok=True)
        stale = os.path.join(folder, "x.png")
        open(stale, "wb").close()
        os.utime(stale, (0, 0))
        self.assertEqual(sa.purge(cfg), 0)
        self.assertTrue(os.path.exists(stale))


if __name__ == "__main__":
    unittest.main()
