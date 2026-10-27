"""看板的数据层：从库里挑出要显示的条目（纯数据，不碰界面，可单测）。"""

from __future__ import annotations

from typing import List

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
    from .core.deadline import label, parse

    due = parse(item.text, now)
    if not due:
        return False
    text = label(due, now)
    return text == "已过期" or text.startswith("今天")
