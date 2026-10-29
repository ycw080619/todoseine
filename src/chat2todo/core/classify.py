"""消息粗分类：闲聊 / 信息 / 行动 / 风险 / 红包 / 转账。

分类只用于降低后续抽取的噪音，不追求精确。
"""

from __future__ import annotations

from ..models import Message, MsgType

RISK_TYPES = {MsgType.LINK, MsgType.LOCATION}

#: 红包可能出现在类型标记里，也可能只是正文措辞（QQ 卡片、纯文本转发）
RED_PACKET_HINTS = (
    "[红包]", "[QQ红包]", "[微信红包]", "QQ红包", "微信红包",
    "拼手气红包", "恭喜发财，大吉大利", "恭喜发财，大吉大利！", "🧧",
)

#: 转账只认类型标记与明确的收款措辞；「记得转账给房东」这类是自己的事，不算
TRANSFER_HINTS = (
    "[转账]", "[QQ转账]", "[微信转账]", "向你转账", "转账成功", "已收款", "待收款",
)

INFO_HINTS = ("通知", "公告", "开会", "会议", "变更", "放假", "值班")


def is_red_packet(msg: Message) -> bool:
    """是不是一条红包消息（含 QQ 卡片类型与正文措辞两种形态）。"""
    if msg.type == MsgType.RED_PACKET:
        return True
    text = msg.content or ""
    return any(hint in text for hint in RED_PACKET_HINTS)


def is_transfer(msg: Message) -> bool:
    """是不是一条别人发来的转账 / 收款消息。"""
    if msg.type == MsgType.TRANSFER:
        return True
    text = msg.content or ""
    return any(hint in text for hint in TRANSFER_HINTS)


def classify(msg: Message) -> str:
    """返回 chat / info / risk / redpacket / transfer / other 之一。"""
    if is_red_packet(msg):
        return "redpacket"
    if is_transfer(msg):
        return "transfer"
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
