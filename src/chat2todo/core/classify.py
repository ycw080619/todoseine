"""消息粗分类：闲聊 / 信息 / 行动 / 风险。

分类只用于降低后续抽取的噪音，不追求精确。
"""

from __future__ import annotations

from ..models import Message, MsgType

RISK_TYPES = {MsgType.RED_PACKET, MsgType.TRANSFER, MsgType.LINK, MsgType.LOCATION}
INFO_HINTS = ("通知", "公告", "开会", "会议", "变更", "放假", "值班")


def classify(msg: Message) -> str:
    """返回 chat / info / risk / other 之一。"""
    if msg.type in RISK_TYPES:
        return "risk"
    if msg.type != MsgType.TEXT:
        return "other"
    text = (msg.content or "").strip()
    if len(text) <= 4:
        return "chat"
    if any(hint in text for hint in INFO_HINTS):
        return "info"
    return "chat"
