"""幂等去重。

同一份导出文件重复导入不会产生重复数据；
不同时间导出的两份文件（时间窗重叠）也不会重复。

已知局限（文档 6.2 已写明）：同一人在同一秒连发两条内容完全相同的消息，
默认会被合并成一条。要保留它们，请把配置项 strict_dedupe 设为 true。
"""

from __future__ import annotations

from typing import Iterable, List

from ..models import Message


def dedupe(messages: Iterable[Message], strict: bool = False) -> List[Message]:
    """按 Message.dedupe_key() 去重，保持原始顺序。

    strict=True 表示不做内存去重（对应配置 strict_dedupe，
    用于保留“同秒同文”；数据库层仍有 UNIQUE 约束兜底）。
    """
    if strict:
        return list(messages)

    seen = set()
    out: List[Message] = []
    for msg in messages:
        key = msg.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(msg)
    return out
