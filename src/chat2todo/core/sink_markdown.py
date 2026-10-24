"""Markdown 输出：可直接放进 Obsidian 或任意笔记库。"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from ..models import TodoItem


def render_markdown(todos: Iterable[TodoItem], title: str = "待办清单") -> str:
    todos = list(todos)
    lines = [f"# {title}", ""]
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}　共 {len(todos)} 条")
    lines.append("")

    groups = [
        ("pending", "待办"),
        ("unconfirmed", "待确认"),
        ("done", "已完成"),
    ]
    for status, heading in groups:
        bucket = [t for t in todos if t.status == status]
        if not bucket:
            continue
        lines.append(f"## {heading}（{len(bucket)}）")
        lines.append("")
        for item in bucket:
            when = datetime.fromtimestamp(item.message_ts).strftime("%m-%d %H:%M")
            box = "x" if status == "done" else " "
            lines.append(f"- [{box}] {item.text}")
            lines.append(
                f"  - {item.session or '未知会话'} · {item.sender} · {when}"
                f" · 置信度 {item.confidence:.2f}"
            )
            if item.reason:
                lines.append(f"  - 依据：{item.reason}")
        lines.append("")

    if not todos:
        lines.append("_没有匹配到待办。_")
    return "\n".join(lines) + "\n"
