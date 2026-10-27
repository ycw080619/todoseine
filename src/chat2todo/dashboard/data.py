"""看板的数据层：从库里挑出要显示的条目（纯数据，不碰界面，可单测）。"""

from __future__ import annotations

from typing import List, Tuple

from ..core import Store
from ..models import TodoItem


def load_items(db_path: str, status: str = "pending") -> List[TodoItem]:
    """给界面用的纯函数：读一批待办，方便无 GUI 环境下测试。

    注意：看板的 "pending" 是「还没做完」的口语含义，
    必须把规则判为「待确认」(unconfirmed) 的条目也包含进来，
    否则 3 分的模糊待办会从看板上凭空消失。
    """
    store = Store(db_path)
    try:
        items = store.list_todos()
    finally:
        store.close()
    if status == "pending":
        from datetime import date

        today = date.today().isoformat()
        # 今天勾掉的条目留在列表里（划线沉底），第二天自动进回收站
        items = [i for i in items
                 if i.status in ("pending", "unconfirmed")
                 or (i.status == "done" and (i.done_at or "") == today)]
    else:
        items = [i for i in items if i.status == status]
    # AI 复核判定为 noise 的条目只是被规则误抓的闲聊，不进看板
    items = [i for i in items if i.status != "noise"]
    # 看板要「最近该做的事」：先按状态（确认过的优先），再按时间倒序
    items.sort(key=lambda i: (i.status == "done", i.status != "pending",
                             -int(i.message_ts or 0)))
    return items


def _is_today_due(item: TodoItem, now=None) -> bool:
    """今日标签：截止时间落在今天或已经过期（只看还没做完的）。"""
    from ..core.deadline import label, parse

    due = parse(item.text, now)
    if not due:
        return False
    text = label(due, now)
    return text == "已过期" or text.startswith("今天")


def due_remindable(items, now=None, within: int = 3600) -> List[Tuple[TodoItem, int]]:
    """挑出「within 秒内到期或刚过期」的未完成条目，返回 [(item, due_ts)] 按到期升序。

    到期提醒专用：已完成的、解析不出截止时间的都不打扰；
    过期太久的 deadline.parse 自己会滤掉（一天前的旧消息不翻账）。
    """
    from datetime import datetime

    from ..core.deadline import parse

    now = now or datetime.now()
    now_ts = now.timestamp()
    out: List[Tuple[TodoItem, int]] = []
    for item in items:
        if item.status == "done":
            continue
        due = parse(item.text, now)
        if due and due - now_ts <= within:
            out.append((item, due))
    out.sort(key=lambda pair: pair[1])
    return out
