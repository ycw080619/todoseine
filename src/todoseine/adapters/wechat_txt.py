"""微信聊天记录 txt 解析器。

微信没有官方纯文本导出，社区常见三种形态，这里都按「宽松匹配」处理::

    2024-01-15 09:15:00 小李          # 行头在上一行，正文下一行
    你好在吗

    2024-01-15 09:15:00 小李: 你好    # 行头与正文同一行（冒号分隔）
    [2024-01-15 09:15:00] 小李: 你好  # 方括号包裹

    小李 2024-01-15 09:15:00          # 名字在前（部分第三方导出）
    你好

解析策略：能匹配到行头就开新消息，剩余部分作为本条正文首行；
匹配不到则把整行追加到当前消息正文里。无法识别的文件会返回空，由上层报 E1002。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterator

from ..models import Message, guess_type
from .base import SourceAdapter

_TS = r"(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)"

# 1) [2024-01-15 09:15:00] 小李: 你好
_INLINE_BRACKET = re.compile(rf"^\[\s*{_TS}\s*\]\s*(?P<name>[^:：]{{1,40}})\s*[:：]\s*(?P<rest>.*)$")
# 2) 2024-01-15 09:15:00 小李: 你好
_INLINE_PLAIN = re.compile(rf"^{_TS}\s+(?P<name>[^:：]{{1,40}})\s*[:：]\s*(?P<rest>.*)$")
# 3) 2024-01-15 09:15:00 小李（正文在下一行）
_HEADER_ONLY = re.compile(rf"^{_TS}\s+(?P<name>[^\s:：][^:：]{{0,39}})\s*$")
# 4) 小李 2024-01-15 09:15:00（名字在前）
_NAME_FIRST = re.compile(rf"^(?P<name>[^\s:：][^:：]{{0,39}})\s+{_TS}\s*$")


def _to_epoch(date_str: str, time_str: str) -> int:
    date_str = date_str.replace("/", "-")
    fmt = "%Y-%m-%d %H:%M:%S" if time_str.count(":") == 2 else "%Y-%m-%d %H:%M"
    return int(datetime.strptime(f"{date_str} {time_str}", fmt).timestamp())


class WeChatTxtAdapter(SourceAdapter):
    name = "wechat_txt"
    extensions = (".txt", ".md")

    def read(self, path: str) -> Iterator[Message]:
        current = None  # (ts, name, [content])

        def flush():
            if current is None:
                return None
            ts, name, buf = current
            content = "\n".join(x for x in buf).strip()
            if not content:
                return None
            return Message(timestamp=ts, sender_id=name, sender_name=name,
                           content=content, type=guess_type(content))

        for raw in self.read_lines(path):
            line = raw.rstrip()
            m = (_INLINE_BRACKET.match(line) or _INLINE_PLAIN.match(line)
                 or _HEADER_ONLY.match(line) or _NAME_FIRST.match(line))
            if m:
                done = flush()
                if done is not None:
                    yield done
                gd = m.groupdict()
                current = (
                    _to_epoch(gd["date"], gd["time"]),
                    gd["name"].strip(),
                    [gd.get("rest", "")] if gd.get("rest") else [],
                )
                continue
            if current is not None:
                current[2].append(line)

        done = flush()
        if done is not None:
            yield done
