"""QQ 导出 txt 解析器。

支持的两种行头（手机 QQ / 电脑 QQ 导出）::

    2024-01-15 14:23:01 老王(12345678)
    2024-01-15 14:23 老王

正文从行头下一行开始，直到下一个行头出现。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterator

from ..models import Message, guess_type
from .base import SourceAdapter

_HEADER_WITH_ID = re.compile(
    r"^(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})\s+"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s+"
    r"(?P<name>.+?)\s*[（(]\s*(?P<uid>\d+)\s*[)）]\s*$"
)
_HEADER_PLAIN = re.compile(
    r"^(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})\s+"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s+"
    r"(?P<name>[^\s(（][^()（）]{0,39})\s*$"
)


def _to_epoch(date_str: str, time_str: str) -> int:
    date_str = date_str.replace("/", "-")
    fmt = "%Y-%m-%d %H:%M:%S" if time_str.count(":") == 2 else "%Y-%m-%d %H:%M"
    return int(datetime.strptime(f"{date_str} {time_str}", fmt).timestamp())


class QQTxtAdapter(SourceAdapter):
    name = "qq_txt"
    extensions = (".txt",)

    def read(self, path: str) -> Iterator[Message]:
        lines = self.read_lines(path)
        current = None  # (ts, name, uid, [content_lines])

        def flush():
            if current is None:
                return None
            ts, name, uid, buf = current
            content = "\n".join(x for x in buf).strip()
            return Message(
                timestamp=ts,
                sender_id=uid or name,
                sender_name=name,
                content=content,
                type=guess_type(content),
            )

        for raw in lines:
            line = raw.rstrip()
            matched = _HEADER_WITH_ID.match(line) or _HEADER_PLAIN.match(line)
            if matched:
                done = flush()
                if done is not None and done.content:
                    yield done
                gd = matched.groupdict()
                current = (
                    _to_epoch(gd["date"], gd["time"]),
                    gd["name"].strip(),
                    (gd.get("uid") or "").strip(),
                    [],
                )
                continue
            if current is not None:
                current[3].append(line)

        done = flush()
        if done is not None and done.content:
            yield done
