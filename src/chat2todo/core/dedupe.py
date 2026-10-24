"""幂等去重。

同一份导出文件重复导入不会产生重复数据；
不同时间导出的两份文件（时间窗重叠）也不会重复。
"""

from __future__ import annotations

from typing import Iterable, List

from ..models import Message


def dedupe(messages: Iterable[Message]) -> List[Message]:
    """按 Message.dedupe_key() 去重，保持原始顺序。"""
    seen = set()
    out: List[Message] = []
    for msg in messages:
        key = msg.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(msg)
    return out
