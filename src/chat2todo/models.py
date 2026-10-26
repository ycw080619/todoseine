"""核心数据模型（零第三方依赖）。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional, Tuple


class MsgType(IntEnum):
    """消息类型枚举，数值与 ChatLab Format 对齐，便于互相导入导出。"""

    TEXT = 0
    IMAGE = 1
    VOICE = 2
    VIDEO = 3
    FILE = 4
    EMOJI = 5
    LINK = 7
    LOCATION = 8
    RED_PACKET = 20
    TRANSFER = 21
    POKE = 22
    CALL = 23
    SHARE = 24
    REPLY = 25
    FORWARD = 26
    CONTACT = 27
    SYSTEM = 80
    RECALL = 81
    OTHER = 99


#: QQ / 微信导出文本里常见的前缀标记 -> 消息类型
MEDIA_PREFIXES = {
    "[图片]": MsgType.IMAGE,
    "[表情]": MsgType.EMOJI,
    "[语音]": MsgType.VOICE,
    "[视频]": MsgType.VIDEO,
    "[文件]": MsgType.FILE,
    "[链接]": MsgType.LINK,
    "[位置]": MsgType.LOCATION,
    "[红包]": MsgType.RED_PACKET,
    "[转账]": MsgType.TRANSFER,
    "[撤回]": MsgType.RECALL,
    "[动画表情]": MsgType.EMOJI,
}


def guess_type(content: str) -> MsgType:
    """根据内容前缀猜测消息类型。"""
    if not content:
        return MsgType.OTHER
    for prefix, msg_type in MEDIA_PREFIXES.items():
        if content.startswith(prefix):
            return msg_type
    return MsgType.TEXT


@dataclass
class Message:
    timestamp: int
    sender_id: str
    sender_name: str
    content: str = ""
    type: MsgType = MsgType.TEXT
    msg_id: Optional[str] = None
    star: bool = False            # 来自「特别关心」群/对象的消息，抽取时标为重点

    @property
    def content_hash(self) -> str:
        return hashlib.sha1((self.content or "").encode("utf-8")).hexdigest()[:16]

    def dedupe_key(self) -> Tuple[int, str, int, str]:
        """去重键：时间戳 + 发送者 + 内容长度 + 内容哈希。

        前三项与 ChatLab 的增量去重规则一致；第四项用于区分
        「同一秒、同一人、等长但内容不同」的两条消息。
        """
        return (self.timestamp, self.sender_id, len(self.content or ""), self.content_hash)


@dataclass
class Session:
    name: str
    platform: str = "qq"
    kind: str = "group"
    messages: List[Message] = field(default_factory=list)


@dataclass
class TodoItem:
    text: str
    session: str
    sender: str
    message_ts: int
    reason: str = ""
    confidence: float = 0.0
    status: str = "pending"
    todo_id: Optional[int] = None

    def as_row(self) -> Tuple:
        return (
            self.text,
            self.session,
            self.sender,
            self.message_ts,
            self.reason,
            self.confidence,
            self.status,
        )

    def as_dict(self) -> dict:
        return {
            "id": self.todo_id,
            "text": self.text,
            "session": self.session,
            "sender": self.sender,
            "ts": self.message_ts,
            "reason": self.reason,
            "confidence": round(float(self.confidence), 2),
            "status": self.status,
        }
