"""ChatLab Format 适配器（JSON / JSONL 均可）。

格式参考: https://docs.chatlab.fun/standard/chatlab-format
本项目与 ChatLab 互不依赖，仅共享交换格式，便于两边互相导入导出。
"""

from __future__ import annotations

import json
from typing import Dict, Iterator

from ..models import Message, MsgType
from .base import SourceAdapter


def _member_map(members) -> Dict[str, str]:
    return {
        str(m.get("platformId", "")): m.get("accountName") or m.get("platformId", "")
        for m in (members or [])
    }


def _to_message(item: dict, names: Dict[str, str]) -> Message:
    sender = str(item.get("sender", ""))
    content = item.get("content") or ""
    try:
        msg_type = MsgType(int(item.get("type", 0)))
    except ValueError:
        msg_type = MsgType.OTHER
    return Message(
        timestamp=int(item.get("timestamp", 0)),
        sender_id=sender,
        sender_name=item.get("accountName") or names.get(sender, sender),
        content=content,
        type=msg_type,
    )


class ChatLabJsonAdapter(SourceAdapter):
    name = "chatlab_json"
    extensions = (".json", ".jsonl")

    def read(self, path: str) -> Iterator[Message]:
        if path.lower().endswith(".jsonl"):
            yield from self._read_jsonl(path)
        else:
            yield from self._read_json(path)

    def _read_json(self, path: str) -> Iterator[Message]:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        names = _member_map(data.get("members"))
        for item in data.get("messages", []):
            yield _to_message(item, names)

    def _read_jsonl(self, path: str) -> Iterator[Message]:
        names: Dict[str, str] = {}
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 坏行跳过，不中断整份导入
                kind = obj.get("_type")
                if kind == "member":
                    names[str(obj.get("platformId", ""))] = obj.get("accountName", "")
                elif kind == "message":
                    yield _to_message(obj, names)
