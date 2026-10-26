"""桌面悬浮看板（tkinter，标准库实现，零第三方依赖）。

功能（对齐主流待办 App 的常见交互）：
- 顶部筛选标签：待办 / 待定 / 今日 / 完成
- 内联快速添加：底部输入框回车即入库，不用弹窗
- 滚轮翻页列表，Ctrl+滚轮调透明度
- 迷你模式：收起成小时钟胶囊，点一下展开
- 置顶开关、撤销上一步（Ctrl+Z）、右键菜单（完成 / 非待办 / 恢复 / 看原文）

设计约束（与方案第五章一致）：
- 只读数据库；勾选/清空时才写回状态，不改原文；
- 置顶常显，但不做任何键鼠模拟，也不主动抢焦点；
- 无图形环境时给出 E3001 与建议，不抛栈。
"""

from __future__ import annotations

import sys
import time
from typing import List, Tuple

from .core import Store
from .models import TodoItem


def _fmt_ts(ts) -> str:
    """把时间戳格式化成 10-25 13:40；拿不到就原样返回。"""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return str(ts or "")

# ---- 外观参数（改这里就能换皮）----
KEY = "#010203"        # 透明色键：窗口外圆角区域用这个颜色，Windows 会把它抠透
PANEL = "#1c1a18"      # 面板底色
TEXT = "#f5f3ef"       # 主文字
SUB = "#b6b1a9"        # 次要文字
LINE = "#4b4641"       # 分隔线
FIELD = "#26231f"      # 输入框底色
ACTIVE = "#332f2b"     # 选中标签底色
ACCENTS = ["#ffffff", "#ff9f43", "#ffd93d", "#6ee7a8",
           "#4cc9f0", "#8aa4ff", "#ff8fab"]

W, H = 360, 540        # 展开态
W_MINI, H_MINI = 200, 108
PAD = 16
TOP = 176              # 列表起始 y
FOOT = 104              # 底部预留（输入框 + 页脚）
ROW = 42               # 单行高度
TABS: List[Tuple[str, str]] = [("todo", "待办"), ("maybe", "待定"),
                               ("today", "今日"), ("done", "完成")]


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


def _is_today_due(item: TodoItem, now=None) -> bool:
    """今日标签：截止时间落在今天或已经过期（只看还没做完的）。"""
    from .core.deadline import label, parse

    due = parse(item.text, now)
    if not due:
        return False
    text = label(due, now)
    return text == "已过期" or text.startswith("今天")


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
        import tkinter  # noqa: F401  仅用于提前判断是否可用
    except ImportError:  # 罕见：Python 未带 Tk
        print("[E3001] 当前 Python 未包含 tkinter，无法打开看板", file=sys.stderr)
        print("  建议：改用 c2t todos / c2t export，或安装带 Tk 的 Python", file=sys.stderr)
        return 1

    try:
        built = _build(db_path, interval=interval, status=status,
                       alpha=alpha, x=x, y=y)
    except Exception as exc:  # noqa: BLE001 - 无图形环境（TclError）走这里
        print(f"[E3001] 无法创建窗口：{exc}", file=sys.stderr)
        return 1
    if built is None:
        return 1
    root = built[0]
    root.mainloop()
    return 0


def _build(db_path: str, interval: int = 30, status: str = "pending",
           alpha: float = 0.93, x: int = 120, y: int = 120):
    """创建看板窗口但**不进入事件循环**，返回 (root, canvas, state, 刷新毫秒)。

    单独抽出来是为了能在无头/自动化环境里驱动界面做回归验证；
    正常运行由 run() 调用后进入 mainloop。
    """
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
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
    _rounded(cv, 1, 1, W - 1, H - 1, 18, fill=PANEL, outline="", tags="bg")

    state = {
        "tab": {"pending": "todo", "unconfirmed": "maybe", "done": "done"}.get(status, "todo"),
        "offset": 0,
        "mini": False,
        "h": H,
        "busy": False,
        "undo": None,          # (todo_id, 原状态)
        "counts": (0, 0),
    }

    # ---- 内联输入框（放在 Canvas 之上，可真正接收键盘）----
    PLACEHOLDER = "＋ 添加一条，回车保存"
    entry = tk.Entry(root, bg=FIELD, fg=TEXT, insertbackground=TEXT, bd=0,
                     highlightthickness=0, font=("Microsoft YaHei UI", 9))
    entry.insert(0, PLACEHOLDER)
    entry.config(fg="#6f6a64")
    ph = {"on": True}

    def ph_off(_evt=None) -> None:
        if ph["on"]:
            entry.delete(0, "end")
            entry.config(fg=TEXT)
            ph["on"] = False

    def ph_on(_evt=None) -> None:
        if not entry.get().strip():
            entry.delete(0, "end")
            entry.insert(0, PLACEHOLDER)
            entry.config(fg="#6f6a64")
            ph["on"] = True

    entry.bind("<FocusIn>", ph_off)
    entry.bind("<FocusOut>", ph_on)

    # ---- 数据操作 ----
    def _store() -> Store:
        return Store(db_path)

    def _mark(item: TodoItem, new_status: str) -> None:
        st = _store()
        try:
            st.set_status(item.todo_id, new_status)
        finally:
            st.close()
        state["undo"] = (item.todo_id, item.status)
        render()

    def toggle_item(item: TodoItem) -> None:
        """圆点：待定→确认，待办→完成，已完成→恢复待办。"""
        if item.status == "unconfirmed":
            _mark(item, "pending")
        elif item.status == "done":
            _mark(item, "pending")
        else:
            _mark(item, "done")

    def undo() -> None:
        u = state["undo"]
        if not u:
            return
        todo_id, prev = u
        st = _store()
        try:
            st.set_status(todo_id, prev)
        finally:
            st.close()
        state["undo"] = None
        render()

    def add_todo(text: str) -> None:
        st = _store()
        try:
            st.add_todo(text)
        finally:
            st.close()
        state["tab"] = "todo"
        state["offset"] = 0
        render()

    def clear_all() -> None:
        if not messagebox.askyesno("清空", "把所有待办标记为完成？", parent=root):
            return
        st = _store()
        try:
            for it in st.list_todos(status="pending"):
                st.set_status(it.todo_id, "done")
        finally:
            st.close()
        state["undo"] = None
        render()

    def show_detail(item: TodoItem) -> None:
        """点文字：弹窗看原文上下文（判断这条该不该做）。"""
        win = tk.Toplevel(root)
        win.title("原文")
        win.configure(bg=PANEL)
        win.attributes("-topmost", True)
        win.geometry(f"380x320+{root.winfo_x() + 40}+{root.winfo_y() + 60}")

        tk.Label(win, text="待办内容", bg=PANEL, fg=SUB, anchor="w",
                 font=("Microsoft YaHei UI", 8)).pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(win, text=item.text, bg=PANEL, fg=TEXT, anchor="w", justify="left",
                 wraplength=350, font=("Microsoft YaHei UI", 10)).pack(
            fill="x", padx=12, pady=(2, 8))

        meta = f"{item.sender or '未知'} · {_fmt_ts(item.message_ts)} · 置信度 {item.confidence:.2f}"
        if item.reason:
            meta += f"\n依据：{item.reason}"
        tk.Label(win, text=meta, bg=PANEL, fg=SUB, anchor="w", justify="left",
                 wraplength=350, font=("Microsoft YaHei UI", 8)).pack(
            fill="x", padx=12, pady=(0, 8))

        tk.Frame(win, bg=LINE, height=1).pack(fill="x", padx=12)
        tk.Label(win, text="前后消息", bg=PANEL, fg=SUB, anchor="w",
                 font=("Microsoft YaHei UI", 8)).pack(fill="x", padx=12, pady=(8, 2))

        box = tk.Text(win, height=8, bg="#141312", fg=TEXT, bd=0, wrap="word",
                      font=("Microsoft YaHei UI", 8), padx=8, pady=6)
        box.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        st = _store()
        try:
            for m in reversed(st.nearby_messages(item.message_ts or 0, 6)):
                who = m.get("sender_name") or "?"
                when = _fmt_ts(m.get("ts"))
                box.insert("end", f"[{when}] {who}\n{m.get('content','')}\n\n")
        except Exception as exc:  # noqa: BLE001
            box.insert("end", f"读取失败：{exc}")
        finally:
            st.close()
        box.config(state="disabled")
        tk.Button(win, text="关闭", command=win.destroy, bg="#2b2926", fg=TEXT,
                  bd=0, activebackground="#3a3733", activeforeground=TEXT).pack(pady=(0, 10))

    # ---- 右键菜单 ----
    menu = tk.Menu(root, tearoff=0, bg="#2b2926", fg=TEXT, bd=0,
                   activebackground=ACTIVE, activeforeground=TEXT)

    def popup(e, item: TodoItem) -> None:
        menu.delete(0, "end")
        if item.status != "done":
            menu.add_command(label="标记完成", command=lambda: _mark(item, "done"))
        else:
            menu.add_command(label="恢复为待办", command=lambda: _mark(item, "pending"))
        menu.add_command(label="标为非待办", command=lambda: _mark(item, "noise"))
        menu.add_separator()
        menu.add_command(label="查看原文", command=lambda: show_detail(item))
        menu_ref["item"] = item
        menu.tk_popup(e.x_root, e.y_root)

    # ---- 窗口拖动 / 收起 ----
    drag = {"x": 0, "y": 0, "moved": False}

    def on_press(e) -> None:
        drag["x"], drag["y"], drag["moved"] = e.x, e.y, False
        cv.focus_set()

    def on_move(e) -> None:
        if abs(e.x - drag["x"]) + abs(e.y - drag["y"]) > 4:
            drag["moved"] = True
        root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")

    def close() -> None:
        root.destroy()

    def toggle_mini() -> None:
        state["mini"] = not state["mini"]
        state["offset"] = 0
        render()

    def toggle_pin() -> None:
        on = not bool(root.attributes("-topmost"))
        root.attributes("-topmost", on)
        render()

    def switch_tab(key: str) -> None:
        if state["tab"] == key:
            return
        state["tab"] = key
        state["offset"] = 0
        render()

    # ---- 尺寸与列表 ----
    def _resize(h: int, w: int = W) -> None:
        if h == state["h"] and w == state["w"]:
            return
        state["h"], state["w"] = h, w
        geo = root.geometry()                      # 形如 360x540+120+120
        pos = geo[geo.find("+"):]
        root.geometry(f"{w}x{h}{pos}")
        cv.config(width=w, height=h)
        cv.delete("bg")
        _rounded(cv, 1, 1, w - 1, h - 1, 18, fill=PANEL, outline="", tags="bg")
        cv.tag_lower("bg")

    state["w"] = W

    # ---- 固定元素：时钟 / 日期 ----
    clock = cv.create_text(W // 2, 66, text="00:00:00", fill=TEXT,
                           font=("Consolas", 30, "bold"), tags="clock")
    date_id = cv.create_text(W // 2, 104, text="", fill=SUB,
                             font=("Microsoft YaHei UI", 9), tags="clock")

    def tick() -> None:
        now = time.localtime()
        week = "日一二三四五六"[now.tm_wday]
        if state["mini"]:
            cv.itemconfig(clock, text=time.strftime("%H:%M", now))
            cv.itemconfig(date_id, text=time.strftime("%m月%d日", now) + f" 周{week}")
        else:
            cv.itemconfig(clock, text=time.strftime("%H:%M:%S", now))
            cv.itemconfig(date_id, text=time.strftime("%Y年%m月%d日", now) + f"　星期{week}")
        root.after(1000, tick)

    def render() -> None:
        if state["busy"]:
            return
        state["busy"] = True
        try:
            cv.delete("body")
            try:
                todo_items = load_items(db_path, "pending")
            except Exception as exc:  # noqa: BLE001 - 界面不能因为读库失败就崩
                todo_items = []
                err = str(exc)
            else:
                err = ""

            unconfirmed = sum(1 for i in todo_items if i.status == "unconfirmed")

            if state["mini"]:
                _render_mini(len(todo_items), unconfirmed)
                return
            _render_full(todo_items, unconfirmed, err)
        finally:
            state["busy"] = False

    def _render_mini(total: int, unconfirmed: int) -> None:
        _resize(H_MINI, W_MINI)
        cv.coords(clock, W_MINI // 2, 42)
        cv.itemconfig(clock, font=("Consolas", 22, "bold"))
        cv.coords(date_id, W_MINI // 2, 68)
        tick_now = time.localtime()
        cv.itemconfig(clock, text=time.strftime("%H:%M", tick_now))
        cv.itemconfig(date_id, text=time.strftime("%m月%d日", tick_now)
                      + f" 周{'日一二三四五六'[tick_now.tm_wday]}")
        cv.create_text(W_MINI // 2, 92,
                       text=f"待办 {total} · 待定 {unconfirmed}", fill=SUB,
                       font=("Microsoft YaHei UI", 8), tags="body")
        cv.create_text(W_MINI - 14, 14, text="✕", anchor="e", fill=SUB,
                       font=("Microsoft YaHei UI", 9), tags=("body", "close"))
        if entry.winfo_ismapped():
            entry.place_forget()

    def _render_full(todo_items: List[TodoItem], unconfirmed: int, err: str) -> None:
        # 先定数据，再定窗口高度（条目少就矮一点，不浪费屏幕）
        if state["tab"] == "today":
            from datetime import datetime

            now = datetime.now()
            items = [i for i in todo_items if _is_today_due(i, now)]
        elif state["tab"] == "maybe":
            items = [i for i in todo_items if i.status == "unconfirmed"]
        elif state["tab"] == "done":
            try:
                items = load_items(db_path, "done")[:30]
            except Exception:  # noqa: BLE001
                items = []
        else:
            items = todo_items

        h = TOP + max(1, min(len(items) or 1, 8)) * ROW + FOOT
        h = max(360, min(h, 660))
        _resize(h, W)

        cv.coords(clock, W // 2, 66)
        cv.itemconfig(clock, font=("Consolas", 30, "bold"))
        cv.coords(date_id, W // 2, 104)

        # 顶部标题栏
        cv.create_text(PAD, 22, text="Chat2Todo", anchor="w",
                       fill=TEXT, font=("Microsoft YaHei UI", 10, "bold"), tags="body")
        cv.create_text(W - 22, 22, text="✕", anchor="e", fill=SUB,
                       font=("Microsoft YaHei UI", 11), tags=("body", "close"))
        cv.create_text(W - 52, 22, text="—", anchor="e", fill=SUB,
                       font=("Microsoft YaHei UI", 11), tags=("body", "mini"))
        pinned = bool(root.attributes("-topmost"))
        cv.create_text(W - 82, 22, text="钉", anchor="e", fill=TEXT if pinned else SUB,
                       font=("Microsoft YaHei UI", 9), tags=("body", "pin"))

        # 筛选标签
        seg = (W - 2 * PAD) / len(TABS)
        for idx, (key, label) in enumerate(TABS):
            x1 = PAD + idx * seg
            active = key == state["tab"]
            if active:
                _rounded(cv, x1 + 2, 134, x1 + seg - 2, 164, 10,
                         fill=ACTIVE, outline="", tags="body")
            cv.create_text(x1 + seg / 2, 149, text=label,
                           fill=TEXT if active else SUB,
                           font=("Microsoft YaHei UI", 9, "bold" if active else "normal"),
                           tags=("body", f"tab{idx}"))
            cv.tag_bind(f"tab{idx}", "<Button-1>", lambda _e, k=key: switch_tab(k))
        cv.create_line(PAD, 172, W - PAD, 172, fill=LINE, tags="body")

        rows_room = max(1, (h - TOP - FOOT) // ROW)
        max_offset = max(0, len(items) - rows_room)
        if state["offset"] > max_offset:
            state["offset"] = max_offset
        visible = items[state["offset"]:state["offset"] + rows_room]

        y = TOP + 12
        if err:
            cv.create_text(PAD, y, text=f"读取失败：{err}", anchor="w", fill="#ff8fab",
                           width=W - 2 * PAD, tags="body")
        elif not items:
            empty = {"todo": "没有待处理的事项 🎉", "maybe": "没有待确认的条目",
                     "today": "今天没有到期的待办", "done": "还没有完成的记录"}[state["tab"]]
            cv.create_text(PAD, y, text=empty, anchor="w", fill=SUB,
                           font=("Microsoft YaHei UI", 9), tags="body")
        else:
            from .core.deadline import label as due_label, parse as due_parse

            for idx, item in enumerate(visible):
                tag = f"item{idx}"
                i = state["offset"] + idx
                if item.status == "done":
                    dot_fill = "#6f6a64"
                elif item.status == "unconfirmed":
                    dot_fill = SUB
                else:
                    dot_fill = ACCENTS[i % len(ACCENTS)]
                # 圆点与 ⋯ 各自独立成标签，避免点圆点时把详情窗一起打开
                cv.create_oval(PAD, y - 4, PAD + 8, y + 4, fill=dot_fill,
                               outline="", tags=("body", f"dot{idx}"))
                due = due_parse(item.text)
                chip = due_label(due)
                # 有到期标签时右侧要留位置，没有则多显示几个字
                limit = 16 if chip else 24
                body = item.text if len(item.text) <= limit else item.text[:limit] + "…"
                if item.status == "unconfirmed":
                    body = "? " + body        # 待定：点一下确认
                elif item.status == "done":
                    body = "✓ " + body
                cv.create_text(PAD + 18, y, text=body, anchor="w",
                               fill=(SUB if item.status != "pending" else TEXT),
                               font=("Microsoft YaHei UI", 9), tags=("body", tag))
                if chip:
                    # 过期红 / 今天黄 / 24 小时内橙 / 其余灰——不把「明天」误标成红
                    now_ts = time.time()
                    if due and due < now_ts:
                        color = "#ff6b6b"
                    elif chip.startswith("今天"):
                        color = "#ffd93d"
                    elif due and due - now_ts <= 86400:
                        color = "#ff9f43"
                    else:
                        color = "#8f8a84"
                    cv.create_text(W - PAD - 34, y, text=chip, anchor="e", fill=color,
                                   font=("Microsoft YaHei UI", 8), tags=("body", tag))
                cv.create_text(W - PAD, y, text="⋯", anchor="e", fill="#6f6a64",
                               font=("Microsoft YaHei UI", 9), tags=("body", f"more{idx}"))
                cv.tag_bind(tag, "<Button-1>", lambda _e, it=item: show_detail(it))
                cv.tag_bind(f"dot{idx}", "<Button-1>",
                            lambda _e, it=item: toggle_item(it))
                cv.tag_bind(f"more{idx}", "<Button-1>",
                            lambda e, it=item: popup(e, it))
                for t in (tag, f"dot{idx}", f"more{idx}"):
                    cv.tag_bind(t, "<Button-3>", lambda e, it=item: popup(e, it))
                cv.create_line(PAD, y + 22, W - PAD, y + 22, fill=LINE, tags="body")
                y += ROW

        # 底部：输入框 + 页脚
        foot_y = h - 28
        cv.create_text(PAD, foot_y, text=f"待办 {len(todo_items)} · 待定 {unconfirmed}",
                       anchor="w", fill="#7d7973", font=("Microsoft YaHei UI", 8), tags="body")
        cv.create_text(W - PAD, foot_y, text="清空", anchor="e", fill=SUB,
                       font=("Microsoft YaHei UI", 8), tags=("body", "clear"))
        cv.tag_bind("clear", "<Button-1>", lambda _e: clear_all())
        if state["undo"]:
            cv.create_text(W - PAD - 44, foot_y, text="撤销", anchor="e", fill="#8aa4ff",
                           font=("Microsoft YaHei UI", 8), tags=("body", "undo"))
            cv.tag_bind("undo", "<Button-1>", lambda _e: undo())

        entry.place(x=PAD + 2, y=h - 70, width=W - 2 * PAD - 12, height=26)

    # ---- 交互绑定 ----
    cv.tag_bind("close", "<Button-1>", lambda _e: close())
    cv.tag_bind("mini", "<Button-1>", lambda _e: toggle_mini())
    cv.tag_bind("pin", "<Button-1>", lambda _e: toggle_pin())

    cv.bind("<Button-1>", on_press)
    cv.bind("<B1-Motion>", on_move)

    def on_release(_e) -> None:
        """迷你模式下轻点任意处展开（拖动不算）。"""
        if state["mini"] and not drag["moved"]:
            current = cv.find_withtag("current")
            if "close" in cv.gettags(current):
                return
            toggle_mini()

    cv.bind("<ButtonRelease-1>", on_release)

    def on_wheel(e) -> str:
        if state["mini"]:
            return "break"
        state["offset"] = max(0, state["offset"] + (-1 if e.delta > 0 else 1))
        render()
        return "break"

    def on_ctrl_wheel(e) -> str:
        cur = float(root.attributes("-alpha"))
        nxt = min(1.0, max(0.35, cur + (0.05 if e.delta > 0 else -0.05)))
        root.attributes("-alpha", round(nxt, 2))
        return "break"

    cv.bind("<MouseWheel>", on_wheel)
    root.bind("<Control-MouseWheel>", on_ctrl_wheel)

    def submit(_evt=None) -> None:
        if ph["on"]:
            return
        text = entry.get().strip()
        if not text:
            return
        entry.delete(0, "end")
        ph_on()
        add_todo(text)

    def _blur(_evt=None) -> str:
        root.focus_set()
        return "break"        # 输入框里按 Esc 只退出输入，不关窗口

    def _new_todo(_evt=None) -> str:
        if state["mini"]:
            toggle_mini()
        ph_off()
        root.after(60, entry.focus_set)
        return "break"        # Ctrl+N：收起时先展开，再把光标放进输入框

    entry.bind("<Return>", submit)
    entry.bind("<Escape>", _blur)

    root.bind("<Escape>", lambda _e: close())
    root.bind("<Control-z>", lambda _e: undo())
    root.bind("<Control-n>", _new_todo)

    tick()
    render()
    refresh_ms = max(5, int(interval)) * 1000

    def loop() -> None:
        render()
        root.after(refresh_ms, loop)

    root.after(refresh_ms, loop)
    return root, cv, state, refresh_ms
