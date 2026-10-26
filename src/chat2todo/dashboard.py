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
import threading
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

# ---- 外观参数（改这里就能换皮；参考 DeskBox 的 WinUI 深色卡片风格）----
KEY = "#010203"        # 透明色键：窗口外圆角区域用这个颜色，Windows 会把它抠透
PANEL = "#212024"      # 面板底色（偏中性深灰，WinUI 深色）
BORDER = "#3a3940"     # 面板描边（1px，模拟 WinUI 卡片边）
TEXT = "#f2f2f4"       # 主文字
SUB = "#9c9ca6"        # 次要文字
LINE = "#332f2f"       # 分隔线（更淡，仅用于必要分组）
FIELD = "#2c2b31"      # 输入框底色
ACTIVE = "#3a3942"     # 选中标签底色
ACCENT = "#4cc2ff"     # WinUI 强调色（用于选中态与链接）
ACCENTS = ["#ff6b6b", "#ff9f43", "#ffd93d", "#6ee7a8",
           "#4cc9f0", "#8aa4ff", "#ff8fab"]

W, H = 360, 540        # 展开态
W_MINI, H_MINI = 200, 108
PAD = 16
TOP = 156              # 列表起始 y（顶部输入卡之下）
FOOT = 54              # 底部预留（只看页脚一行）
ROW = 62               # 单行高度（卡片式）
TABS: List[Tuple[str, str]] = [("todo", "全部"), ("star", "重点"),
                               ("today", "今日"), ("maybe", "待定"), ("done", "完成")]


def _single_instance(name: str = "Chat2TodoDashboard") -> bool:
    """用命名互斥量做单实例，避免开机自启重复拉起。"""
    try:
        import ctypes

        ctypes.windll.kernel32.CreateMutexW(None, False, name)
        already = ctypes.windll.kernel32.GetLastError() == 183   # ERROR_ALREADY_EXISTS
        return not already
    except Exception:  # noqa: BLE001
        return True


from .core.hotkey import parse_hotkey  # noqa: E402


def _start_hotkey(callback, mods: int = 0x0003 | 0x4000, vk: int = 0x44) -> None:
    """注册全局热键 Ctrl+Alt+D（MOD_ALT|MOD_CONTROL|MOD_NOREPEAT），后台线程收消息。"""
    def loop() -> None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            if not user32.RegisterHotKey(None, 1, mods, vk):
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == 0x0312:      # WM_HOTKEY
                    callback()
        except Exception:  # noqa: BLE001
            return

    threading.Thread(target=loop, daemon=True).start()


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


def _hwnd_of(root) -> int:
    """拿到顶层窗口句柄（overrideredirect 下 winfo_id 可能是子窗口）。"""
    try:
        import ctypes

        wid = root.winfo_id()
        user32 = ctypes.windll.user32
        anc = user32.GetAncestor(wid, 2)      # GA_ROOT：取真正的顶层窗口
        return anc or wid
    except Exception:  # noqa: BLE001
        return 0


def _set_layer(root, keep_topmost: bool) -> None:
    """贴桌面层 / 置顶二选一。

    keep_topmost=False 时把窗口压到 Z 序最底（HWND_BOTTOM），
    效果是「贴在桌面上、不挡别的窗口」；True 则常驻最上层。
    """
    try:
        import ctypes

        hwnd = _hwnd_of(root)
        if not hwnd:
            return
        root.attributes("-topmost", keep_topmost)
        hwnd_insert_after = -1 if keep_topmost else 1   # HWND_TOPMOST / HWND_BOTTOM
        flags = 0x0001 | 0x0002 | 0x0010                # NOSIZE|NOMOVE|NOACTIVATE
        ctypes.windll.user32.SetWindowPos(hwnd, hwnd_insert_after, 0, 0, 0, 0, flags)
    except Exception:  # noqa: BLE001
        pass


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
    if not _single_instance():
        print("看板已在运行（Ctrl+Alt+D 可唤出）", file=sys.stderr)
        return 0
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
    root.attributes("-topmost", False)   # 默认贴桌面层，点「钉」才置顶
    try:
        root.attributes("-alpha", alpha)
        root.attributes("-transparentcolor", KEY)
    except tk.TclError:
        pass                              # 非 Windows 平台优雅降级为方形
    root.configure(bg=KEY)
    root.geometry(f"{W}x{H}+{x}+{y}")

    cv = tk.Canvas(root, width=W, height=H, bg=KEY, highlightthickness=0)
    cv.pack()
    root.after(150, lambda: _set_layer(root, False))
    _rounded(cv, 1, 1, W - 1, H - 1, 18, fill=PANEL, outline=BORDER, width=1, tags="bg")

    state = {
        "tab": {"pending": "todo", "unconfirmed": "maybe", "done": "done"}.get(status, "todo"),
        "offset": 0,
        "mini": False,
        "h": H,
        "busy": False,
        "undo": None,          # (todo_id, 原状态)
        "counts": (0, 0),
        "pin": False,          # False=贴桌面层，True=置顶
    }

    # ---- 内联输入框（放在 Canvas 之上，可真正接收键盘）----
    PLACEHOLDER = "添加待办"
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
        state["pin"] = not bool(state.get("pin"))
        _set_layer(root, state["pin"])
        render()

    def reveal() -> None:
        """热键唤出：每次都浮到最前；闲置 15 秒后自动沉回桌面层（钉住则常驻）。"""
        if state.get("pin"):
            return
        state["reveal_on"] = True
        state["reveal_until"] = time.time() + 15
        _set_layer(root, True)
        try:
            root.lift()
        except Exception:  # noqa: BLE001
            pass

    # 全局热键只置一个标志，真正操作在 UI 线程的 tick 里做（避免跨线程动 Tk）
    hotkey_spec = "ctrl+alt+d"
    try:
        from pathlib import Path

        from .config import load as _load_cfg

        # 注意：config.load 的默认查找路径不含 ~/Chat2Todo/c2t.toml，必须显式传
        cfg_path = Path.home() / "Chat2Todo" / "c2t.toml"
        cfg_data = _load_cfg(str(cfg_path)) if cfg_path.exists() else _load_cfg()
        hotkey_spec = ((cfg_data.get("dash") or {}).get("hotkey") or hotkey_spec)
    except Exception:  # noqa: BLE001
        pass
    parsed = parse_hotkey(hotkey_spec)
    if parsed:
        _start_hotkey(lambda: state.__setitem__("reveal", True), parsed[0], parsed[1])

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
        _rounded(cv, 1, 1, w - 1, h - 1, 18, fill=PANEL, outline=BORDER, width=1, tags="bg")
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
        try:
            cv.itemconfig("hclock", text=time.strftime("%H:%M", now))
        except Exception:  # noqa: BLE001 - 标题栏不在（收起态）时忽略
            pass
        if state["mini"]:
            cv.itemconfig(clock, text=time.strftime("%H:%M", now))
            cv.itemconfig(date_id, text=time.strftime("%m月%d日", now) + f" 周{week}")
        else:
            cv.itemconfig(clock, text=time.strftime("%H:%M:%S", now))
            cv.itemconfig(date_id, text=time.strftime("%Y年%m月%d日", now) + f"　星期{week}")
        if state.pop("reveal", False):
            reveal()
        # 唤出状态到期 → 收起；平时贴桌面层，被点浮上来也会被压回去
        if state.get("reveal_on") and time.time() >= state.get("reveal_until", 0):
            state["reveal_on"] = False
        # 贴桌面层时要定期压回去：否则被人点一下就会浮到前面挡住别的窗口
        if not state.get("pin") and not state.get("reveal_on"):
            try:
                focused = root.focus_get()
            except Exception:  # noqa: BLE001
                focused = None
            if focused not in (entry,):
                _set_layer(root, False)
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
        cv.itemconfig(clock, state="normal")
        cv.itemconfig(date_id, state="normal")
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
        elif state["tab"] == "star":
            items = [i for i in todo_items if "★重点" in (i.reason or "")]
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

        # 大时钟只在收起态显示；展开态把时间收进标题栏，给列表让出空间
        cv.itemconfig(clock, text="", state="hidden")
        cv.itemconfig(date_id, text="", state="hidden")

        # 标题栏：方形图标 + 标题 +（右侧）时间 / 钉 / — / ✕
        _rounded(cv, PAD, 14, PAD + 20, 34, 6, fill=ACCENT, outline="", tags="body")
        cv.create_text(PAD + 10, 24, text="≡", fill="#0b1a22",
                       font=("Microsoft YaHei UI", 9, "bold"), tags="body")
        cv.create_text(PAD + 28, 24, text="待办", anchor="w",
                       fill=TEXT, font=("Microsoft YaHei UI", 11, "bold"), tags="body")
        cv.create_text(W - 110, 24, text=time.strftime("%H:%M"), anchor="e", fill=SUB,
                       font=("Consolas", 10), tags=("body", "hclock"))
        cv.create_text(W - 22, 24, text="✕", anchor="e", fill=SUB,
                       font=("Microsoft YaHei UI", 11), tags=("body", "close"))
        cv.create_text(W - 50, 24, text="—", anchor="e", fill=SUB,
                       font=("Microsoft YaHei UI", 11), tags=("body", "mini"))
        pinned = bool(state.get("pin"))
        cv.create_text(W - 76, 24, text="钉", anchor="e", fill=ACCENT if pinned else SUB,
                       font=("Microsoft YaHei UI", 9), tags=("body", "pin"))

        # 顶部输入卡（借鉴参考图：先说「添加待办」，再列条目）
        _rounded(cv, PAD, 48, W - PAD, 88, 12, fill=FIELD, outline=BORDER, width=1, tags="body")
        cv.create_text(PAD + 18, 68, text="＋", anchor="center", fill=SUB,
                       font=("Microsoft YaHei UI", 12), tags="body")

        # 筛选标签：圆角胶囊 + 数量（DeskBox 的 All / Today / ... 风格）
        counts = {
            "todo": len(todo_items),
            "maybe": unconfirmed,
            "star": sum(1 for i in todo_items if "★重点" in (i.reason or "")),
            "today": sum(1 for i in todo_items if _is_today_due(i)),
            "done": None,
        }
        # 分段控件：整体一块胶囊底，选中格高亮 + 强调色下划线
        _rounded(cv, PAD, 98, W - PAD, 134, 12, fill=FIELD, outline="", tags="body")
        seg = (W - 2 * PAD) / len(TABS)
        for idx, (key, label) in enumerate(TABS):
            x1 = PAD + idx * seg
            active = key == state["tab"]
            n = counts.get(key)
            text = f"{label} {n}" if n else label
            cx = x1 + seg / 2
            if active:
                _rounded(cv, x1 + 3, 101, x1 + seg - 3, 131, 10,
                         fill=ACTIVE, outline="", tags="body")
            cv.create_text(cx, 113, text=text,
                           fill=TEXT if active else SUB,
                           font=("Microsoft YaHei UI", 9, "bold" if active else "normal"),
                           tags=("body", f"tab{idx}"))
            if active:
                cv.create_line(cx - 11, 126, cx + 11, 126, fill=ACCENT, width=2,
                               capstyle="round", tags=("body", f"tab{idx}"))
            cv.tag_bind(f"tab{idx}", "<Button-1>", lambda _e, k=key: switch_tab(k))

        rows_room = max(1, (h - TOP - FOOT) // ROW)
        max_offset = max(0, len(items) - rows_room)
        if state["offset"] > max_offset:
            state["offset"] = max_offset
        visible = items[state["offset"]:state["offset"] + rows_room]

        y = TOP + 14
        if err:
            cv.create_text(PAD, y, text=f"读取失败：{err}", anchor="w", fill="#ff8fab",
                           width=W - 2 * PAD, tags="body")
        elif not items:
            empty = {"todo": "暂无待办", "star": "还没有重点条目",
                     "maybe": "没有待确认的条目",
                     "today": "今天没有到期的待办", "done": "还没有完成的记录"}[state["tab"]]
            hint = {"todo": "在下方添加，或等同步把聊天里的活儿捞进来",
                    "star": "在控制台把某人/某个群设为「特别关心」即可",
                    "maybe": "规则拿不准的条目会先放这里，点一下确认",
                    "today": "今天没有截止项，轻松一点",
                    "done": "完成一条就会出现在这里"}[state["tab"]]
            cv.create_text(W / 2, y + 30, text=empty, anchor="center", fill=TEXT,
                           font=("Microsoft YaHei UI", 10), tags="body")
            cv.create_text(W / 2, y + 54, text=hint, anchor="center", fill=SUB,
                           width=W - 2 * PAD, font=("Microsoft YaHei UI", 8), tags="body")
        else:
            from .core.deadline import label as due_label, parse as due_parse

            for idx, item in enumerate(visible):
                tag = f"item{idx}"
                i = state["offset"] + idx
                if item.status == "done":
                    bar = "#6f6a64"
                elif item.status == "unconfirmed":
                    bar = "#ffd93d"
                else:
                    bar = ACCENTS[i % len(ACCENTS)]
                # 卡片：圆角底 + 1px 描边，勾选框 / 色条 / 标题 / 说明 / ⋯
                _rounded(cv, PAD, y - 26, W - PAD, y + 26, 14,
                         fill=FIELD, outline=BORDER, width=1, tags="body")
                # 勾选框（点击切换完成）
                if item.status == "done":
                    _rounded(cv, PAD + 12, y - 9, PAD + 30, y + 9, 5,
                             fill=ACCENT, outline="", tags=("body", f"dot{idx}"))
                    cv.create_text(PAD + 21, y, text="✓", fill="#0b1a22",
                                   font=("Microsoft YaHei UI", 9, "bold"),
                                   tags=("body", f"dot{idx}"))
                else:
                    _rounded(cv, PAD + 12, y - 9, PAD + 30, y + 9, 5,
                             fill="", outline="#6d6c74", width=1,
                             tags=("body", f"dot{idx}"))
                # 左侧竖色条
                cv.create_line(PAD + 38, y - 13, PAD + 38, y + 11, fill=bar,
                               width=3, capstyle="round", tags="body")
                due = due_parse(item.text)
                chip = due_label(due)
                limit = 20
                body = item.text if len(item.text) <= limit else item.text[:limit] + "…"
                if "★重点" in (item.reason or ""):
                    body = "★ " + body
                cv.create_text(PAD + 48, y - 9, text=body, anchor="w",
                               fill=(SUB if item.status != "pending" else TEXT),
                               font=("Microsoft YaHei UI", 9, "bold"), tags=("body", tag))
                # 第二行：状态 / 到期说明
                meta_bits = []
                if item.status == "unconfirmed":
                    meta_bits.append("待确认")
                elif item.status == "done":
                    meta_bits.append("已完成")
                else:
                    meta_bits.append("规则判定")
                if chip:
                    meta_bits.append(chip)
                now_ts = time.time()
                if due and due < now_ts:
                    meta_color = "#ffd93d"
                elif chip.startswith("今天"):
                    meta_color = "#ffd93d"
                elif due and due - now_ts <= 86400:
                    meta_color = "#ff9f43"
                else:
                    meta_color = SUB
                cv.create_text(PAD + 48, y + 10, text=" · ".join(meta_bits), anchor="w",
                               fill=meta_color, font=("Microsoft YaHei UI", 7),
                               tags=("body", tag))
                cv.create_text(W - PAD - 14, y - 8, text="⋯", anchor="e", fill="#6f6a64",
                               font=("Microsoft YaHei UI", 9), tags=("body", f"more{idx}"))

                cv.tag_bind(tag, "<Button-1>", lambda _e, it=item: show_detail(it))
                cv.tag_bind(f"dot{idx}", "<Button-1>",
                            lambda _e, it=item: toggle_item(it))
                cv.tag_bind(f"more{idx}", "<Button-1>",
                            lambda e, it=item: popup(e, it))


                for t in (tag, f"dot{idx}", f"more{idx}"):
                    cv.tag_bind(t, "<Button-3>", lambda e, it=item: popup(e, it))
                y += ROW

        # 页脚：左边统计，右边「清除已完成」胶囊按钮
        foot_y = h - 26
        cv.create_text(PAD, foot_y, text=f"待办 {len(todo_items)} · 待定 {unconfirmed}",
                       anchor="w", fill=SUB, font=("Microsoft YaHei UI", 8), tags="body")
        if state["undo"]:
            cv.create_text(PAD + 92, foot_y, text="撤销", anchor="w", fill=ACCENT,
                           font=("Microsoft YaHei UI", 8), tags=("body", "undo"))
            cv.tag_bind("undo", "<Button-1>", lambda _e: undo())
        btn_text = "清除已完成"
        bw = 72
        _rounded(cv, W - PAD - bw, foot_y - 11, W - PAD, foot_y + 11, 11,
                 fill=FIELD, outline=BORDER, width=1, tags=("body", "clear"))
        cv.create_text(W - PAD - bw / 2, foot_y, text=btn_text, anchor="center", fill=SUB,
                       font=("Microsoft YaHei UI", 8), tags=("body", "clear"))
        cv.tag_bind("clear", "<Button-1>", lambda _e: clear_all())

        # 输入框压在顶部「添加待办」卡片上
        entry.place(x=PAD + 42, y=57, width=W - 2 * PAD - 52, height=22)

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
