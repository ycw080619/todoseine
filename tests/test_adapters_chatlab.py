"""ChatLab Format 双向 roundtrip：导出→再导入，消息数和内容一致。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from todoseine.adapters.chatlab_json import ChatLabJsonAdapter
from todoseine.models import Message, MsgType


class TestChatLabRoundtrip(unittest.TestCase):
    def _make_msgs(self):
        return [
            Message(timestamp=1, sender_id="u1", sender_name="张三",
                    content="明天记得开会", type=MsgType.TEXT),
            Message(timestamp=2, sender_id="u2", sender_name="李四",
                    content="好的收到", type=MsgType.TEXT),
            Message(timestamp=3, sender_id="u1", sender_name="张三",
                    content="[图片]", type=MsgType.IMAGE),
        ]

    def test_json_roundtrip(self):
        adapter = ChatLabJsonAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.json")
            n = adapter.write(path, self._make_msgs())
            self.assertEqual(n, 3)

            # 再导入
            msgs = list(adapter.read(path))
            self.assertEqual(len(msgs), 3)
            self.assertEqual(msgs[0].sender_name, "张三")
            self.assertEqual(msgs[0].content, "明天记得开会")
            self.assertEqual(msgs[1].sender_id, "u2")
            self.assertEqual(msgs[2].type, MsgType.IMAGE)

    def test_jsonl_roundtrip(self):
        adapter = ChatLabJsonAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.jsonl")
            adapter.write(path, self._make_msgs())

            msgs = list(adapter.read(path))
            self.assertEqual(len(msgs), 3)
            # JSONL 里 member 行被 _read_jsonl 收集进 names，
            # message 的 sender_name 应该能从 member 里查到
            self.assertEqual(msgs[0].sender_name, "张三")
            self.assertEqual(msgs[2].content, "[图片]")

    def test_json_has_schema_and_members(self):
        adapter = ChatLabJsonAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.json")
            adapter.write(path, self._make_msgs())
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["schema"], "chatlab-format-v1")
            # members 去重：u1, u2 两个
            ids = {m["platformId"] for m in data["members"]}
            self.assertEqual(ids, {"u1", "u2"})
            self.assertEqual(len(data["messages"]), 3)

    def test_jsonl_member_lines_first(self):
        adapter = ChatLabJsonAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.jsonl")
            adapter.write(path, self._make_msgs())
            with open(path, encoding="utf-8") as fh:
                lines = [json.loads(ln) for ln in fh if ln.strip()]
            # 前两行是 member，后三行是 message
            self.assertEqual([ln["_type"] for ln in lines[:2]], ["member", "member"])
            self.assertTrue(all(ln["_type"] == "message" for ln in lines[2:]))
            self.assertEqual(len(lines), 5)

    def test_other_adapters_no_write(self):
        """qq_txt / wechat_txt 不实现 write，应该抛 NotImplementedError。"""
        from todoseine.adapters import REGISTRY

        for name in ("qq_txt", "wechat_txt"):
            adapter = REGISTRY[name]()
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "x.txt")
                with self.assertRaises(NotImplementedError):
                    adapter.write(path, [])

    def test_chat_id_preserved_in_json(self):
        adapter = ChatLabJsonAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.json")
            adapter.write(path, self._make_msgs(), chat_id="g123")
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["messages"][0]["chatId"], "g123")
