"""桌面悬浮看板（tkinter，标准库实现，零第三方依赖）。

样式参考：半透明圆角面板 + 大号时钟 + 日期 + 彩色圆点待办列表 + 底部添加/清空。

设计约束（与方案第五章一致）：
- 只读数据库；勾选/清空时才写回状态，不改原文；
- 置顶常显，但不做任何键鼠模拟，也不主动抢焦点；
- 无图形环境时给出 E3001 与建议，不抛栈。
"""

from __future__ import annotations

import sys
import time
from typing import List

from .core import Store
from .models import TodoItem

# ---- 外观参数（改这里就能换皮）----
KEY = "#010203"        # 透明色键：窗口外圆角区域用这个颜色，Windows 会把它抠透
PANEL = "#1c1a18"      # 面板底色
TEXT = "#f5f3ef"       # 主文字
SUB = "#b6b1a9"        # 次要文字
LINE = "#4b4641"       # 分隔线
ACCENTS = ["#ffffff", "#ff9f43", "#ffd93d", "#6ee7a8",
           "#4cc9f0", "#8aa4ff", "#ff8fab"]

W, H = 360, 540
PAD = 16


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
        items = [i for i in items if i.status in ("pending", "unconfirmed")]
    else:
        items = [i for i in items if i.status == status]
    # AI 复核判定为 noise 的条目只是被规则误抓的闲聊，不进看板
    items = [i for i in items if i.status != "noise"]
    # 看板要「最近该做的事」：先按状态（确认过的优先），再按时间倒序
    items.sort(key=lambda i: (i.status != "pending", -int(i.message_ts or 0)))
    return items


def _rounded(canvas, x1, y1, x2, y2, r, **kw):
    """用平滑多边形画圆角矩形（避免依赖第三方绘图库）。"""
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


def run(db_path: str, interval: int = 30, status: str = "pending",
        alpha: float = 0.93, x: int = 120, y: int = 120) -> int:
    """启动悬浮看板，阻塞直到关闭。"""
    try:
        import tkinter as tk
        from tkinter import messagebox, simpledialog
    except ImportError:  # 罕见：Python 未带 Tk
        print("[E3001] 当前 Python 未包含 tkinter，无法打开看板", file=sys.stderr)
        print("  建议：改用 c2t todos / c2t export，或安装带 Tk 的 Python", file=sys.stderr)
        return 1

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"[E3001] 无法创建窗口：{exc}", file=sys.stderr)
        return 1

    root.overrideredirect(True)          # 无边框
    root.attributes("-topmost", True)    # 置顶
    try:
        root.attributes("-alpha", alpha)
        root.attributes("-transparentcolor", KEY)
    except tk.TclError:
        pass                              # 非 Windows 平台优雅降级为方形
    root.configure(bg=KEY)
    root.geometry(f"{W}x{H}+{x}+{y}")

    cv = tk.Canvas(root, width=W, height=H, bg=KEY, highlightthickness=0)
    cv.pack()
    _rounded(cv, 1, 1, W - 1, H - 1, 18, fill=PANEL, outline="")

    drag = {"x": 0, "y": 0, "moved": False}

    def close() -> None:
        root.destroy()

    def on_press(e) -> None:
        drag["x"], drag["y"], drag["moved"] = e.x, e.y, False

    def on_move(e) -> None:
        if abs(e.x - drag["x"]) + abs(e.y - drag["y"]) > 4:
            drag["moved"] = True
        root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")

    # 顶部：标题 + 收起/关闭
    cv.create_text(PAD, 22, text="Chat2Todo", anchor="w",
                   fill=TEXT, font=("Microsoft YaHei UI", 10, "bold"))
    cv.create_text(W - 22, 22, text="✕", anchor="e", fill=SUB,
                   font=("Microsoft YaHei UI", 11), tags="close")
    cv.tag_bind("close", "<Button-1>", lambda _e: close())

    # 时钟
    clock = cv.create_text(W // 2, 66, text="00:00:00", fill=TEXT,
                           font=("Consolas", 30, "bold"))
    date_id = cv.create_text(W // 2, 104, text="", fill=SUB,
                             font=("Microsoft YaHei UI", 9))
    cv.create_line(PAD, 124, W - PAD, 124, fill=LINE)

    def tick() -> None:
        now = time.localtime()
        cv.itemconfig(clock, text=time.strftime("%H:%M:%S", now))
        week = "日一二三四五六"[now.tm_wday]
        cv.itemconfig(date_id, text=time.strftime("%Y年%m月%d日", now) + f"　星期{week}")
        root.after(1000, tick)

    def set_done(todo_id: int) -> None:
        store = Store(db_path)
        try:
            store.set_status(todo_id, "done")
        finally:
            store.close()
        refresh()

    def set_noise(todo_id: int) -> None:
        """右键：这条不是待办，标成 noise，以后不再出现在看板。"""
        store = Store(db_path)
        try:
            store.set_status(todo_id, "noise")
        finally:
            store.close()
        refresh()

    def add_todo() -> None:
        text = simpledialog.askstring("添加待办", "要做什么？", parent=root)
        if not text or not text.strip():
            return
        store = Store(db_path)
        try:
            store.add_todo(text.strip())
        finally:
            store.close()
        refresh()

    def clear_all() -> None:
        if not messagebox.askyesno("清空", "把所有待办标记为完成？", parent=root):
            return
        store = Store(db_path)
        try:
            for it in store.list_todos(status="pending"):
                store.set_status(it.todo_id, "done")
        finally:
            store.close()
        refresh()

    state = {"busy": False, "h": H}

    def _resize(n: int) -> int:
        """按条目数自适应高度，让面板像贴纸一样贴着内容，而不是留一大片空白。"""
        h = max(300, 150 + n * 42 + 76)
        if h == state["h"]:
            return h
        state["h"] = h
        geo = root.geometry()                      # 形如 360x540+120+120
        pos = geo[geo.find("+"):]
        root.geometry(f"{W}x{h}{pos}")
        cv.config(height=h)
        cv.delete("bg")
        _rounded(cv, 1, 1, W - 1, h - 1, 18, fill=PANEL, outline="", tags="bg")
        cv.tag_lower("bg")
        return h

    def refresh() -> None:
        if state["busy"]:
            return
        state["busy"] = True
        try:
            cv.delete("body")
            try:
                items = load_items(db_path, status)
            except Exception as exc:  # noqa: BLE001 - 界面不能因为读库失败就崩
                cv.create_text(PAD, 150, text=f"读取失败：{exc}", anchor="w",
                               fill="#ff8fab", width=W - 2 * PAD, tags="body")
                items = []
            h = _resize(min(len(items), 8))
            y = 148
            if not items:
                cv.create_text(PAD, y, text="没有待处理的事项 🎉", anchor="w",
                               fill=SUB, font=("Microsoft YaHei UI", 9), tags="body")
            from .core.deadline import label as due_label, parse as due_parse, urgency as due_urgency

            items = sorted(
                items[:40],
                key=lambda i: (i.status != "pending", due_parse(i.text) or 9e18, -int(i.message_ts or 0)),
            )
            for i, item in enumerate(items[:8]):
                tag = f"item{i}"
                cv.create_oval(PAD, y - 4, PAD + 8, y + 4, fill=ACCENTS[i % len(ACCENTS)],
                               outline="", tags=("body", tag))
                due = due_parse(item.text)
                chip = due_label(due)
                text = item.text if len(item.text) <= 17 else item.text[:17] + "…"
                cv.create_text(PAD + 18, y, text=text, anchor="w", fill=TEXT,
                               font=("Microsoft YaHei UI", 9),
                               tags=("body", tag))
                if chip:
                    color = {2: "#ff6b6b", 1: "#ffd93d", 0: "#8f8a84"}[due_urgency(due)]
                    cv.create_text(W - PAD, y, text=chip, anchor="e", fill=color,
                                   font=("Microsoft YaHei UI", 8), tags=("body", tag))
                cv.tag_bind(tag, "<Button-1>",
                            lambda _e, tid=item.todo_id: set_done(tid))
                cv.tag_bind(tag, "<Button-3>",
                            lambda _e, tid=item.todo_id: set_noise(tid))
                cv.create_line(PAD, y + 22, W - PAD, y + 22, fill=LINE, tags="body")
                y += 42

            cv.create_text(PAD, h - 26, text="＋ 添加", anchor="w", fill=SUB,
                           font=("Microsoft YaHei UI", 9), tags=("body", "add"))
            cv.tag_bind("add", "<Button-1>", lambda _e: add_todo())
            cv.create_text(W - PAD, h - 26, text="清空", anchor="e", fill=SUB,
                           font=("Microsoft YaHei UI", 9), tags=("body", "clear"))
            cv.tag_bind("clear", "<Button-1>", lambda _e: clear_all())
            cv.create_text(W // 2, h - 48, text=f"共 {len(items)} 条 · {interval}s 刷新 · Esc 关闭",
                           fill="#7d7973", font=("Microsoft YaHei UI", 7), tags="body")
        finally:
            state["busy"] = False
            root.after(max(5, interval) * 1000, refresh)

    cv.bind("<Button-1>", lambda e: (on_press(e), cv.focus_set()))
    cv.bind("<B1-Motion>", on_move)
    root.bind("<Escape>", lambda _e: close())

    tick()
    refresh()
    root.mainloop()
    return 0
