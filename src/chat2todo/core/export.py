"""导出器：把待办渲染成 Markdown / CSV / JSON（标准库实现，无外部依赖）。"""

from __future__ import annotations

import csv
import io
import json
from typing import Iterable, List

from ..models import TodoItem

CSV_FIELDS = ["id", "text", "session", "sender", "ts", "reason", "confidence", "status"]


def to_csv(todos: Iterable[TodoItem]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_FIELDS)
    for t in todos:
        writer.writerow([
            getattr(t, "todo_id", ""), t.text, t.session, t.sender, t.message_ts,
            t.reason, f"{t.confidence:.2f}", t.status,
        ])
    return buf.getvalue()


def to_json(todos: Iterable[TodoItem], meta: dict | None = None) -> str:
    payload = {
        "generator": "chat2todo",
        "meta": meta or {},
        "todos": [t.as_dict() for t in todos],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def to_markdown_rows(todos: Iterable[TodoItem]) -> str:
    """给终端用的紧凑表格（双视图里清单侧）。"""
    lines: List[str] = []
    for t in todos:
        mark = {"pending": " ", "unconfirmed": "?", "done": "x"}.get(t.status, " ")
        lines.append(f"[{mark}] #{getattr(t, 'todo_id', '?'):<4} {t.text}")
    return "\n".join(lines)
