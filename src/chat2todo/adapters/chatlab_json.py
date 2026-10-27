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

    # --- 导出 -----------------------------------------------------
    def write(self, path: str, messages: Iterable[Message], **kwargs) -> int:
        """把消息写成 ChatLab Format（JSON 或 JSONL，按后缀决定）。

        kwargs:
            chat_id:  可选，会作为每条 message 的 chatId 写出（保留群归属）
            members:  可选，显式成员列表 [{platformId, accountName}]；
                      不传则从消息里自动收集 sender_id/sender_name 去重
        """
        msgs = list(messages)
        if path.lower().endswith(".jsonl"):
            return self._write_jsonl(path, msgs, **kwargs)
        return self._write_json(path, msgs, **kwargs)

    @staticmethod
    def _build_members(msgs: list, extras=None) -> list:
        seen = {}
        for m in msgs:
            pid = m.sender_id or ""
            if pid and pid not in seen:
                seen[pid] = m.sender_name or pid
        if extras:
            for entry in extras:
                pid = str(entry.get("platformId", ""))
                if pid and pid not in seen:
                    seen[pid] = entry.get("accountName", pid)
        return [{"platformId": k, "accountName": v} for k, v in seen.items()]

    @staticmethod
    def _msg_to_dict(m: Message, chat_id: str = "") -> dict:
        item = {
            "sender": m.sender_id,
            "accountName": m.sender_name,
            "content": m.content,
            "type": int(m.type),
            "timestamp": m.timestamp,
        }
        if chat_id:
            item["chatId"] = chat_id
        return item

    def _write_json(self, path: str, msgs: list, **kwargs) -> int:
        payload = {
            "schema": "chatlab-format-v1",
            "members": self._build_members(msgs, kwargs.get("members")),
            "messages": [self._msg_to_dict(m, kwargs.get("chat_id", "")) for m in msgs],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return len(msgs)

    def _write_jsonl(self, path: str, msgs: list, **kwargs) -> int:
        chat_id = kwargs.get("chat_id", "")
        with open(path, "w", encoding="utf-8") as fh:
            # 先写成员
            for member in self._build_members(msgs, kwargs.get("members")):
                obj = dict(member)
                obj["_type"] = "member"
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
            # 再写消息
            for m in msgs:
                obj = self._msg_to_dict(m, chat_id)
                obj["_type"] = "message"
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        return len(msgs)

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
