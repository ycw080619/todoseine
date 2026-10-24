"""数据库只读通道测试。

说明：真实 QQ 库是腾讯自研 WCDB 加密格式，目前没有可用的密钥，
所以这里不伪造解密；测试覆盖的是「探测 → 后端选择 → 行转 Message → 入库」
这条通路本身，用假后端证明接线正确。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from chat2todo.core import Store  # noqa: E402
from chat2todo.errors import C2TError  # noqa: E402
from chat2todo.sources import db as dbmod  # noqa: E402


def _make_fake_db(tmp: str) -> str:
    """伪造一个「1024 头 + N×4096 页」的加密库，只为验证探测逻辑。"""
    path = os.path.join(tmp, "nt_msg.db")
    with open(path, "wb") as fh:
        head = bytearray(1024)
        head[0:16] = b"SQLite header 3\x00"
        head[32:41] = b"QQ_NT DB\x00"
        fh.write(head)
        fh.write(b"\x00" * (4096 * 3))
    return path


class FakeBackend:
    name = "fake"

    def __init__(self, rows):
        self.rows = rows

    def available(self) -> bool:
        return True

    def fetch(self, info, limit=0):
        return self.rows


class TestDBProbe(unittest.TestCase):
    def test_inspect_detects_encrypted_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_fake_db(tmp)
            info = dbmod.inspect(path)
            self.assertTrue(info.encrypted)
            self.assertEqual(info.pages, 3)
            self.assertFalse(info.readable_by_sqlite)

    def test_locate_finds_db_under_account_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            acc = Path(tmp) / "8688858" / "nt_qq" / "nt_db"
            acc.mkdir(parents=True)
            (acc / "nt_msg.db").write_bytes(b"\x00" * 16)
            found = dbmod.locate(tmp)
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "nt_msg.db")

    def test_no_backend_raises_readable_error(self) -> None:
        with self.assertRaises(C2TError) as ctx:
            dbmod.get_backend({"db": {}})
        self.assertEqual(ctx.exception.code, "E4002")


class TestDBPipeline(unittest.TestCase):
    def test_rows_become_messages(self) -> None:
        rows = [
            {"timestamp": 1700000000, "sender": "1001", "sender_name": "小李",
             "content": "明天前把周报发我", "type": 0},
            {"timestamp": 1700000060, "sender": "1002", "sender_name": "小张",
             "text": "记得报名", "type": 0},
            {"timestamp": 1700000120, "sender": "1003", "content": ""},  # 空内容应丢弃
        ]
        msgs = dbmod._rows_to_messages(rows)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].sender_name, "小李")
        self.assertEqual(msgs[1].content, "记得报名")

    def test_load_messages_uses_configured_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_fake_db(tmp)
            fake = FakeBackend([
                {"timestamp": 1700000000, "sender": "1", "sender_name": "A",
                 "content": "明天前把周报发我"},
            ])
            original = dbmod.get_backend
            dbmod.get_backend = lambda cfg: fake  # type: ignore[assignment]
            try:
                msgs = dbmod.load_messages({"db": {"path": path}})
            finally:
                dbmod.get_backend = original
            self.assertEqual(len(msgs), 1)
            self.assertIn("周报", msgs[0].content)

    def test_messages_can_be_stored(self) -> None:
        """通路最后一环：DB 来的消息能进同一个 Store，和文件导入共用一份数据。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "c2t.db")
            fake = FakeBackend([
                {"timestamp": 1700000000, "sender": "1", "sender_name": "A",
                 "content": "明天前把周报发我"},
            ])
            original = dbmod.get_backend
            dbmod.get_backend = lambda cfg: fake  # type: ignore[assignment]
            try:
                msgs = dbmod.load_messages({"db": {"path": _make_fake_db(tmp)}})
            finally:
                dbmod.get_backend = original
            store = Store(db_path)
            try:
                added = store.add_messages(msgs)
                self.assertEqual(added, 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
