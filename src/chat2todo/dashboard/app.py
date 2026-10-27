"""看板主模块：窗口搭建、渲染循环、外观配置与热重载（由单文件 dashboard.py 拆出）。"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from typing import List, Tuple

from ..core import Store
from ..core.hotkey import parse_hotkey
from ..models import TodoItem
from .data import _is_today_due, due_remindable, filter_items, load_items
from .files import _extract_filenames, _find_downloaded_file, _fmt_ts
from .geom import (SNAP_PX, _anchor_of, _apply_anchor, _clamp_to_rects,
                   _ease_out_cubic, _monitor_work_areas, _slide_offset,
                   _snap_to_edges)
from .win32 import _apply_blur, _set_layer, _single_instance, _start_hotkey

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
RADIUS = 18            # 面板圆角，可由 [ui] radius 覆盖
BORDER_ON = True       # 是否画 1px 描边（[ui] border）
BORDER_W = 1           # 面板描边宽度（[ui] border_width，0-4）
SHOW_CLOCK = True      # 标题栏是否显示时间（[ui] clock）
ANIM_ON = True         # 唤出淡入 / 尺寸缓动动画（[ui] anim）
BLUR_ON = False        # Win10+ 毛玻璃（[ui] blur，实验性，与透明色键可能冲突）
SNAP_ON = True         # 拖动松手时吸附工作区边缘（[ui] snap）
HOVER_ON = False       # 迷你胶囊悬停展开 / 离开自动收回（[ui] hover_expand，默认关）
RESIZE_MARGIN = 6      # 左右边缘多宽的区域算「拖边框改宽度」
W_MIN, W_MAX = 280, 560   # 看板宽度可调范围（与 [ui] width 的钳位一致）
W_MINI, H_MINI = 200, 108
PAD = 16
TOP = 156              # 列表起始 y（顶部输入卡之下）
FOOT = 54              # 底部预留（只看页脚一行）
ROW = 62               # 单行高度（卡片式）
TABS: List[Tuple[str, str]] = [("todo", "全部"), ("star", "重点"),
                               ("today", "今日"), ("maybe", "待定"), ("done", "完成")]


def _rounded(canvas, x1, y1, x2, y2, r, **kw):
    """用平滑多边形画圆角矩形（避免依赖第三方绘图库）。"""
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


def _draw_bg(cv, w: int, h: int) -> None:
    """重画面板底（圆角 + 描边）；建窗 / 改尺寸 / 热重载三处共用。"""
    cv.delete("bg")
    _rounded(cv, 1, 1, w - 1, h - 1, RADIUS, fill=PANEL,
             outline=BORDER if BORDER_ON else PANEL,
             width=BORDER_W if BORDER_ON else 0, tags="bg")
    cv.tag_lower("bg")


def _ui_config() -> dict:
    """读 c2t.toml 的 [ui] 段；读不到就返回空字典。"""
    try:
        from pathlib import Path

        from ..config import load

        cfg_path = Path.home() / "Chat2Todo" / "c2t.toml"
        if not cfg_path.exists():
            return {}
        return (load(str(cfg_path)) or {}).get("ui") or {}
    except Exception:  # noqa: BLE001
        return {}


def _pos_file():
    from pathlib import Path

    return Path.home() / "Chat2Todo" / "dash_pos.json"


def load_pos():
    """读上次的窗口状态（位置 / 宽度 / 锁定开关）；没有就返回 None。"""
    try:
        import json

        data = json.loads(_pos_file().read_text(encoding="utf-8"))
        out = {"x": int(data["x"]), "y": int(data["y"]),
               "lock_pos": bool(data.get("lock_pos")),
               "lock_size": bool(data.get("lock_size"))}
        if data.get("w"):
            out["w"] = max(W_MIN, min(W_MAX, int(data["w"])))
        if data.get("mx") is not None:
            out["mx"], out["my"] = int(data["mx"]), int(data["my"])
        a = data.get("anchor")
        if a:
            out["anchor"] = (str(a[0]), int(a[1]), int(a[2]))
        return out
    except Exception:  # noqa: BLE001
        return None


def save_pos(x: int, y: int, w=None, lock_pos=None, lock_size=None,
             mini: bool = False, anchor=None) -> None:
    """记住窗口状态，下次启动回到这里。没传的键保留文件里的旧值。

    mini=True 时 (x, y) 写进迷你胶囊自己的坐标（mx/my），不碰展开态的。
    anchor 是 (角落, 边距x, 边距y)，换分辨率后按它还原相对位置。
    """
    try:
        import json

        f = _pos_file()
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        if mini:
            data["mx"], data["my"] = int(x), int(y)
        else:
            data["x"], data["y"] = int(x), int(y)
        if w is not None:
            data["w"] = int(w)
        if lock_pos is not None:
            data["lock_pos"] = bool(lock_pos)
        if lock_size is not None:
            data["lock_size"] = bool(lock_size)
        if anchor is not None:
            data["anchor"] = [anchor[0], int(anchor[1]), int(anchor[2])]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _config_mtime() -> float:
    """c2t.toml 的修改时间，用于热重载判断。"""
    try:
        from pathlib import Path

        p = Path.home() / "Chat2Todo" / "c2t.toml"
        return p.stat().st_mtime if p.exists() else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _apply_ui_config() -> dict:
    """按配置覆盖调色板全局变量，返回启动参数（透明度）。"""
    import re as _re

    global ACCENT, PANEL, BORDER, TEXT, SUB, LINE, FIELD, ACTIVE
    global RADIUS, W, BORDER_ON, SHOW_CLOCK, BORDER_W, ANIM_ON, BLUR_ON, SNAP_ON
    global HOVER_ON

    ui = _ui_config()
    theme = str(ui.get("theme") or "dark").lower()
    # 每次热重载都先铺回默认值，避免上一次主题/配色的残留
    ACCENT = "#4cc2ff"
    if theme == "light":
        PANEL, BORDER = "#f8f8fb", "#e3e3ec"
        TEXT, SUB, LINE = "#1f2330", "#6b7280", "#ececf3"
        FIELD, ACTIVE = "#ffffff", "#e8eefc"
    else:
        PANEL, BORDER = "#212024", "#3a3940"
        TEXT, SUB, LINE = "#f2f2f4", "#9c9ca6", "#332f2f"
        FIELD, ACTIVE = "#2c2b31", "#3a3942"

    accent = str(ui.get("accent") or "").strip()
    if _re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        ACCENT = accent
    panel = str(ui.get("panel") or "").strip()
    if _re.fullmatch(r"#[0-9a-fA-F]{6}", panel):
        PANEL = panel
    border_color = str(ui.get("border_color") or "").strip()
    if _re.fullmatch(r"#[0-9a-fA-F]{6}", border_color):
        BORDER = border_color
    try:
        alpha = float(ui.get("opacity", 0.93))
    except (TypeError, ValueError):
        alpha = 0.93
    try:
        RADIUS = max(0, min(30, int(ui.get("radius", RADIUS))))
    except (TypeError, ValueError):
        pass
    try:
        W = max(W_MIN, min(W_MAX, int(ui.get("width", W))))
    except (TypeError, ValueError):
        pass
    try:
        BORDER_W = max(0, min(4, int(ui.get("border_width", 1))))
    except (TypeError, ValueError):
        BORDER_W = 1
    BORDER_ON = str(ui.get("border", "true")).lower() not in ("false", "0", "no")
    SHOW_CLOCK = str(ui.get("clock", "true")).lower() not in ("false", "0", "no")
    ANIM_ON = str(ui.get("anim", "true")).lower() not in ("false", "0", "no")
    BLUR_ON = str(ui.get("blur", "false")).lower() in ("true", "1", "yes")
    SNAP_ON = str(ui.get("snap", "true")).lower() not in ("false", "0", "no")
    HOVER_ON = str(ui.get("hover_expand", "false")).lower() in ("true", "1", "yes")
    return {"alpha": max(0.35, min(1.0, alpha))}


def run(db_path: str, interval: int = 30, status: str = "pending",
        alpha: float = None, x: int = 120, y: int = 120) -> int:
    """启动悬浮看板，阻塞直到关闭。"""
    global W

    ui = _apply_ui_config()
    if alpha is None:
        alpha = ui["alpha"]
    saved = load_pos()
    if saved:
        if saved.get("w"):
            W = saved["w"]          # 用户上次拖出来的宽度优先于配置默认值
        if (x, y) == (120, 120):
            x, y = saved["x"], saved["y"]
            anchor = saved.get("anchor")
            rects = _monitor_work_areas()
            if anchor and rects:
                # 按锚点在今天的显示器拓扑里重算位置；拓扑没变时算出来还是原坐标
                cx, cy = x + W // 2, y + H // 2
                target = None
                for rc in rects:
                    if rc[0] <= cx < rc[2] and rc[1] <= cy < rc[3]:
                        target = rc
                        break
                if target is None:
                    target = min(rects, key=lambda rc: (
                        max(rc[0] - cx, 0, cx - rc[2]) ** 2
                        + max(rc[1] - cy, 0, cy - rc[3]) ** 2))
                x, y = _apply_anchor(anchor, W, H, target)
    # 副屏拔掉/换位置后坐标可能落在虚空里，启动时钳回可见工作区
    x, y = _clamp_to_rects(x, y, W, H, _monitor_work_areas())
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
    if saved:
        built[2]["lock_pos"] = bool(saved.get("lock_pos"))
        built[2]["lock_size"] = bool(saved.get("lock_size"))
        if saved.get("mx") is not None:
            built[2]["pos_mini"] = (saved["mx"], saved["my"])
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

    import tkinter.font as tkfont

    FONT_ITEM = tkfont.Font(family="Microsoft YaHei UI", size=9, weight="bold")
    FONT_ITEM_DONE = tkfont.Font(family="Microsoft YaHei UI", size=9,
                                 weight="bold", overstrike=1)

    cv = tk.Canvas(root, width=W, height=H, bg=KEY, highlightthickness=0)
    cv.pack()
    root.after(150, lambda: _set_layer(root, False))
    if BLUR_ON:
        root.after(300, lambda: _apply_blur(root, True))
    _draw_bg(cv, W, H)

    state = {
        "tab": {"pending": "todo", "unconfirmed": "maybe", "done": "done"}.get(status, "todo"),
        "offset": 0,
        "mini": False,
        "h": H,
        "alpha": alpha,      # 目标透明度（动画终点 / Ctrl+滚轮 会同步更新）
        "busy": False,
        "undo": None,          # (todo_id, 原状态)
        "counts": (0, 0),
        "pin": False,          # False=贴桌面层，True=置顶
        "lock_pos": False,     # 锁定位置：拖动无效（右键菜单开关）
        "lock_size": False,    # 锁定尺寸：拖边缘改宽度无效
        "cfg_mtime": None,     # 配置修改时间，用于外观热重载
        "batch": False,        # 批量模式
        "sel": set(),          # 批量模式下选中的 todo_id
        "pos_full": None,      # 展开态记住的位置（迷你胶囊有自己独立的位置）
        "pos_mini": None,
        "hover_open": False,   # 由悬停展开的：指针离开 0.8s 自动收回
        "search": False,       # 搜索模式：顶部输入卡变成搜索框
        "query": "",           # 当前搜索词（大小写不敏感子串过滤）
    }

    def housekeeping() -> None:
        """把「不是今天完成的」条目移进回收站，并按保留天数清空回收站。"""
        try:
            from pathlib import Path

            from ..config import load as _load_cfg

            days = 30
            try:
                cfg_path = Path.home() / "Chat2Todo" / "c2t.toml"
                cfg_data = _load_cfg(str(cfg_path)) if cfg_path.exists() else _load_cfg()
                days = int((cfg_data.get("ui") or {}).get("trash_days", 30))
            except Exception:  # noqa: BLE001
                pass
            st = Store(db_path)
            try:
                st.archive_completed()
                st.purge_trash(days)
            finally:
                st.close()
        except Exception:  # noqa: BLE001
            pass

    root.after(400, housekeeping)

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

    # ---- 搜索框（Ctrl+F 唤出，与添加框共用顶部输入卡的位置）----
    SPLACEHOLDER = "搜索待办 / 发送人，Esc 退出"
    sentry = tk.Entry(root, bg=FIELD, fg=TEXT, insertbackground=TEXT, bd=0,
                      highlightthickness=0, font=("Microsoft YaHei UI", 9))
    sentry.insert(0, SPLACEHOLDER)
    sentry.config(fg="#6f6a64")
    sph = {"on": True}

    def sph_off(_evt=None) -> None:
        if sph["on"]:
            sentry.delete(0, "end")
            sentry.config(fg=TEXT)
            sph["on"] = False

    def sph_on(_evt=None) -> None:
        if not sentry.get().strip():
            sentry.delete(0, "end")
            sentry.insert(0, SPLACEHOLDER)
            sentry.config(fg="#6f6a64")
            sph["on"] = True

    sentry.bind("<FocusIn>", sph_off)
    sentry.bind("<FocusOut>", sph_on)

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

    def toggle_batch() -> None:
        """进入/退出批量模式。"""
        state["batch"] = not state["batch"]
        state["sel"] = set()
        if state["batch"]:
            search_close()               # 批量操作条要占输入卡，先退出搜索
        render()

    def search_open() -> None:
        """Ctrl+F：顶部输入卡切换成搜索框（收起态先展开）。"""
        if state["mini"]:
            toggle_mini()
        if state["batch"]:
            state["batch"] = False
            state["sel"] = set()
        state["search"] = True
        render()
        root.after(60, lambda: (sph_off(), sentry.focus_set()))

    def search_close(_evt=None) -> str:
        """Esc：清空搜索词并退回「添加待办」框。"""
        if not state.get("search"):
            return "break"
        state["search"] = False
        state["query"] = ""
        sentry.delete(0, "end")
        sph_on()
        render()
        root.focus_set()
        return "break"

    def search_type(_evt=None) -> None:
        """边敲边过滤；清空搜索词即恢复完整列表。"""
        if sph["on"]:
            return
        state["query"] = sentry.get()
        state["offset"] = 0
        render()

    def toggle_sel(item) -> None:
        tid = item.todo_id
        if tid is None:
            return
        if tid in state["sel"]:
            state["sel"].discard(tid)
        else:
            state["sel"].add(tid)
        render()

    def batch_select_all() -> None:
        """全选当前可见条目；已经全选则清空（再按一次＝全不选）。"""
        ids = [i.todo_id for i in state.get("visible", []) if i.todo_id is not None]
        if ids and state["sel"].issuperset(ids):
            state["sel"] = set()
        else:
            state["sel"] = set(ids)
        render()

    def open_settings() -> None:
        """打开控制台并直接落到「常规」页。"""
        import subprocess
        from pathlib import Path as _Path

        root_dir = _Path(__file__).resolve().parents[2]
        console = root_dir / "tools" / "qq_db_export" / "console.py"
        if not console.exists():
            return
        try:
            subprocess.Popen([sys.executable, str(console), "--page", "general"],
                             cwd=str(root_dir))
        except Exception:  # noqa: BLE001
            pass

    def batch_action(tag: str) -> None:
        sel = list(state["sel"])
        if tag == "bcancel":
            state["batch"] = False
            state["sel"] = set()
            render()
            return
        if not sel:
            return
        st = _store()
        try:
            if tag == "bdone":
                n = st.set_status_many(sel, "done")
                msg = f"已批量标记完成 {n} 条"
            else:
                n = st.trash_many(sel)
                msg = f"已批量移到回收站 {n} 条"
        finally:
            st.close()
        state["batch"] = False
        state["sel"] = set()
        render()
        try:
            self_write = _store()
            self_write.close()
        except Exception:  # noqa: BLE001
            pass

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

    def edit_item(item: TodoItem) -> None:
        """点行尾「改」：弹小窗改待办文字，回车 / 保存写回数据库。"""
        win = tk.Toplevel(root)
        win.title("修改待办")
        win.configure(bg=PANEL)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.geometry(f"380x132+{root.winfo_x() + 40}+{root.winfo_y() + 60}")

        tk.Label(win, text="待办内容", bg=PANEL, fg=SUB, anchor="w",
                 font=("Microsoft YaHei UI", 8)).pack(fill="x", padx=12, pady=(10, 0))
        var = tk.StringVar(value=item.text)
        ent = tk.Entry(win, textvariable=var, bg=FIELD, fg=TEXT, bd=0,
                       insertbackground=TEXT, font=("Microsoft YaHei UI", 10))
        ent.pack(fill="x", padx=12, pady=(4, 10), ipady=5)
        ent.focus_set()
        ent.select_range(0, "end")

        def _save() -> None:
            text = var.get().strip()
            if text and text != item.text:
                st = _store()
                try:
                    st.update_text(item.todo_id, text)
                finally:
                    st.close()
                render()
            win.destroy()

        ent.bind("<Return>", lambda _e: _save())
        tk.Button(win, text="保存", command=_save, bg=ACCENT, fg="#0b1a22", bd=0,
                  activebackground=ACCENT, activeforeground="#0b1a22",
                  font=("Microsoft YaHei UI", 9)).pack(pady=(0, 10))

    def locate_source(item: TodoItem) -> None:
        """双击：源头提到 QQ 下载的文件时，直接在目录里选中它；否则看原文。

        找文件要遍历 QQ 下载目录，放后台线程，找完回主线程开窗口。
        """
        def work() -> None:
            texts = [item.text or ""]
            try:
                st = _store()
                try:
                    for m in st.nearby_messages(item.message_ts or 0, 2):
                        texts.append(m.get("content", ""))
                finally:
                    st.close()
            except Exception:  # noqa: BLE001
                pass
            path = _find_downloaded_file(_extract_filenames(texts))
            root.after(0, lambda: _open_hit(path))

        def _open_hit(path: str) -> None:
            if path:
                try:
                    subprocess.Popen(["explorer", f"/select,{path}"])
                    return
                except Exception:  # noqa: BLE001
                    pass
            show_detail(item)

        threading.Thread(target=work, daemon=True).start()

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
        menu.add_command(label="修改内容", command=lambda: edit_item(item))
        menu.add_command(label="查看原文", command=lambda: show_detail(item))
        menu_ref["item"] = item
        menu.tk_popup(e.x_root, e.y_root)

    def popup_win(e) -> None:
        """背景右键：窗口级开关（对齐 DeskBox 悬停按钮里的位置/尺寸锁定）。"""
        if any(t.startswith(("item", "dot", "more", "edit"))
               for t in cv.gettags("current")):
            return                      # 条目上的右键走 popup
        menu.delete(0, "end")
        lp = tk.BooleanVar(value=bool(state.get("lock_pos")))
        ls = tk.BooleanVar(value=bool(state.get("lock_size")))

        def _flip(key: str, var) -> None:
            state[key] = bool(var.get())
            _persist()

        menu.add_checkbutton(label="锁定位置", variable=lp,
                             command=lambda: _flip("lock_pos", lp))
        menu.add_checkbutton(label="锁定尺寸", variable=ls,
                             command=lambda: _flip("lock_size", ls))
        menu.tk_popup(e.x_root, e.y_root)

    # ---- 窗口拖动 / 拖边缘改宽 / 收起 ----
    drag = {"x": 0, "y": 0, "moved": False, "mode": "move",
            "sx": 0, "sw": 0, "wx": 0}

    def _geom_pos() -> Tuple[int, int]:
        """从 geometry() 解析窗口位置（未映射时 winfo_x/y 会返回 0）。"""
        import re as _re

        m = _re.search(r"([+-]\d+)([+-]\d+)$", root.geometry())
        return (int(m.group(1)), int(m.group(2))) if m else (120, 120)

    def _apply_width(nw: int, nx: int = None) -> None:
        """拖动边缘时的逐帧宽度反馈：只重画背景，条目重排等松手后的 render。"""
        global W

        nw = max(W_MIN, min(W_MAX, int(nw)))
        if nw == state.get("w", W):
            return
        W = nw
        state["w"] = nw
        h = state.get("h", H)
        if nx is None:
            _set_size(nw, h)                       # 拖右缘：位置不动
        else:                                      # 拖左缘：右边界保持不动
            _gx, gy = _geom_pos()
            root.geometry(f"{nw}x{h}+{nx}+{gy}")
            cv.config(width=nw, height=h)
            _draw_bg(cv, nw, h)

    def on_press(e) -> None:
        _touch_raise()                 # 交互时临时抬到前面（DeskBox 的交互抬层级）
        if state.get("hover_open"):
            state["hover_open"] = False   # 上手操作了，就不再自动收回
        drag["x"], drag["y"], drag["moved"] = e.x, e.y, False
        w_now = state.get("w", W)
        if (not state["mini"] and not state.get("lock_size")
                and (e.x <= RESIZE_MARGIN or e.x >= w_now - RESIZE_MARGIN)):
            # 左右边缘 6px 内按下 = 拖边框改宽度（对齐 DeskBox 的尺寸调整）
            drag["mode"] = "resize-l" if e.x <= RESIZE_MARGIN else "resize-r"
            drag["sx"], drag["sw"] = e.x_root, w_now
            drag["wx"] = _geom_pos()[0]
        elif state.get("lock_pos"):
            drag["mode"] = "none"                  # 位置已锁定，拖动无效
        else:
            drag["mode"] = "move"
        cv.focus_set()

    def on_move(e) -> None:
        if abs(e.x - drag["x"]) + abs(e.y - drag["y"]) > 4:
            drag["moved"] = True
        mode = drag.get("mode", "move")
        if mode == "none":
            return
        if mode == "move":
            root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")
        elif mode == "resize-r":
            _apply_width(drag["sw"] + (e.x_root - drag["sx"]))
        else:  # resize-l：宽度反向变化，左边界跟着指针走
            nw = max(W_MIN, min(W_MAX, drag["sw"] - (e.x_root - drag["sx"])))
            _apply_width(nw, nx=drag["wx"] + (drag["sw"] - nw))

    def on_hover(e) -> None:
        """指针贴近左右边缘时换成水平拖宽光标。"""
        if state["mini"] or state.get("lock_size"):
            cv.config(cursor="")
            return
        w_now = state.get("w", W)
        edge = e.x <= RESIZE_MARGIN or e.x >= w_now - RESIZE_MARGIN
        try:
            cv.config(cursor="sb_h_double_arrow" if edge else "")
        except Exception:  # noqa: BLE001
            pass

    def remember_pos() -> None:
        _persist()

    def on_enter(_e) -> None:
        """悬停展开（[ui] hover_expand）：指针在迷你胶囊上停 0.4s 自动展开。"""
        state["leave_gen"] = state.get("leave_gen", 0) + 1   # 取消待收回
        if not HOVER_ON or not state["mini"]:
            return
        state["hover_gen"] = state.get("hover_gen", 0) + 1
        gen = state["hover_gen"]

        def _open() -> None:
            if state.get("hover_gen") == gen and state["mini"]:
                state["hover_open"] = True
                toggle_mini()

        root.after(400, _open)         # 防误触延迟（对齐 DeskBox Balanced 思路）

    def on_leave(_e) -> None:
        """悬停展开的窗口：指针离开 0.8s 自动收回迷你态。"""
        state["hover_gen"] = state.get("hover_gen", 0) + 1   # 取消待展开
        if not (HOVER_ON and state.get("hover_open") and not state["mini"]):
            return
        state["leave_gen"] = state.get("leave_gen", 0) + 1
        gen = state["leave_gen"]

        def _close() -> None:
            if state.get("leave_gen") != gen:
                return
            if state["mini"] or not state.get("hover_open"):
                return
            if root.focus_get() is entry:   # 还在输入框里打字就不收
                return
            state["hover_open"] = False
            toggle_mini()

        root.after(800, _close)

    def _persist() -> None:
        """把当前窗口状态写进 dash_pos.json：按当前模式写对应坐标，顺手记四角锚点。"""
        try:
            gx, gy = _geom_pos()
            anchor = None
            rects = _monitor_work_areas()
            cx, cy = gx + state.get("w", W) // 2, gy + state.get("h", H) // 2
            for rc in rects:
                if rc[0] <= cx < rc[2] and rc[1] <= cy < rc[3]:
                    anchor = _anchor_of(gx, gy, state.get("w", W),
                                        state.get("h", H), rc)
                    break
            save_pos(gx, gy, w=W, lock_pos=state.get("lock_pos"),
                     lock_size=state.get("lock_size"),
                     mini=bool(state["mini"]), anchor=anchor)
        except Exception:  # noqa: BLE001
            pass

    def close() -> None:
        remember_pos()
        root.destroy()

    def toggle_mini() -> None:
        # 两个模式各记各的位置（DeskBox 的 CompactPlacement）
        state["pos_mini" if state["mini"] else "pos_full"] = _geom_pos()
        # 先捕获当前位置（动画起点），再切模式（render 会启动尺寸动画）
        sx, sy = _geom_pos()
        state["mini"] = not state["mini"]
        state["offset"] = 0
        render()                       # _resize 会自动做尺寸帧动画
        dest = state.get("pos_mini" if state["mini"] else "pos_full")
        if not dest:
            return
        nx, ny = _clamp_to_rects(dest[0], dest[1],
                                 state.get("w", W), state.get("h", H),
                                 _monitor_work_areas())
        # 位置动画：和 _resize 的 14 帧同步 ease_out_cubic
        if not ANIM_ON or (abs(nx - sx) + abs(ny - sy)) < 8:
            root.geometry(f"+{nx}+{ny}")
            return
        state["slide_gen"] = state.get("slide_gen", 0) + 1
        gen = state["slide_gen"]
        frames = 14

        def _step(i: int) -> None:
            if state.get("slide_gen") != gen:
                return
            t = _ease_out_cubic(i / frames)
            try:
                root.geometry(
                    f"+{round(sx + (nx - sx) * t)}"
                    f"+{round(sy + (ny - sy) * t)}")
            except Exception:  # noqa: BLE001
                return
            if i < frames:
                root.after(16, lambda: _step(i + 1))

        _step(1)

    def toggle_pin() -> None:
        state["pin"] = not bool(state.get("pin"))
        _set_layer(root, state["pin"])
        render()

    def _fade_in() -> None:
        """唤出时从半透淡入到目标透明度（约 210ms，ease-out cubic）。"""
        if not ANIM_ON:
            return
        target = float(state.get("alpha", 0.93))
        start = max(0.35, target - 0.25)
        state["fade_gen"] = state.get("fade_gen", 0) + 1
        gen = state["fade_gen"]
        frames = 13                       # ≈210ms，对齐 DeskBox 详情页进入时长

        def _step(i: int) -> None:
            if state.get("fade_gen") != gen:
                return                       # 被更新的唤出/热重载打断
            try:
                root.attributes("-alpha", round(
                    start + (target - start) * _ease_out_cubic(i / frames), 3))
            except Exception:  # noqa: BLE001
                return
            if i < frames:
                root.after(16, lambda: _step(i + 1))

        _step(0)

    def _touch_raise() -> None:
        """交互时临时抬到前面，15 秒无操作沉回（DeskBox 的交互抬层级）。"""
        if state.get("pin"):
            return
        state["fade_gen"] = state.get("fade_gen", 0) + 1   # 打断进行中的沉回淡出
        try:
            root.attributes("-alpha", float(state.get("alpha", 0.93)))
        except Exception:  # noqa: BLE001
            pass
        state["reveal_on"] = True
        state["reveal_until"] = time.time() + 15
        _set_layer(root, True)

    def _slide_in() -> None:
        """唤出时从最近屏幕边缘滑入 24px（约 210ms，与淡入同步）。"""
        if not ANIM_ON:
            return
        fx, fy = _geom_pos()
        sx, sy = _slide_offset(fx, fy, state.get("w", W), state.get("h", H),
                               _monitor_work_areas())
        if (sx, sy) == (fx, fy):
            return
        state["slide_gen"] = state.get("slide_gen", 0) + 1
        gen = state["slide_gen"]
        frames = 13                      # ≈210ms，与 _fade_in 同步
        root.geometry(f"+{sx}+{sy}")

        def _step(i: int) -> None:
            if state.get("slide_gen") != gen:
                return                   # 被更新的唤出打断
            t = _ease_out_cubic(i / frames)
            try:
                root.geometry(f"+{round(sx + (fx - sx) * t)}"
                              f"+{round(sy + (fy - sy) * t)}")
            except Exception:  # noqa: BLE001
                return
            if i < frames:
                root.after(16, lambda: _step(i + 1))

        _step(1)

    def _sink_fade() -> None:
        """沉回桌面层之前先淡出（约 150ms）；动画关就直接沉。"""
        if not ANIM_ON:
            return
        target = float(state.get("alpha", 0.93))
        end = max(0.35, target - 0.25)
        state["fade_gen"] = state.get("fade_gen", 0) + 1
        gen = state["fade_gen"]
        frames = 9

        def _step(i: int) -> None:
            if state.get("fade_gen") != gen:
                return                   # 被新的唤出/交互打断，由对方接管透明度
            try:
                root.attributes("-alpha", round(
                    target + (end - target) * _ease_out_cubic(i / frames), 3))
            except Exception:  # noqa: BLE001
                return
            if i < frames:
                root.after(16, lambda: _step(i + 1))
            else:
                _set_layer(root, False)
                try:
                    root.attributes("-alpha", target)   # 沉到底层后恢复常态透明度
                except Exception:  # noqa: BLE001
                    pass

        _step(0)

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
        _fade_in()
        _slide_in()

    # 全局热键只置一个标志，真正操作在 UI 线程的 tick 里做（避免跨线程动 Tk）
    hotkey_spec = "ctrl+alt+d"
    cfg_data = {}
    try:
        from pathlib import Path

        from ..config import load as _load_cfg

        # 注意：config.load 的默认查找路径不含 ~/Chat2Todo/c2t.toml，必须显式传
        cfg_path = Path.home() / "Chat2Todo" / "c2t.toml"
        cfg_data = _load_cfg(str(cfg_path)) if cfg_path.exists() else _load_cfg()
        hotkey_spec = ((cfg_data.get("dash") or {}).get("hotkey") or hotkey_spec)
    except Exception:  # noqa: BLE001
        pass
    parsed = parse_hotkey(hotkey_spec)
    if parsed:
        _start_hotkey(lambda: state.__setitem__("reveal", True), parsed[0], parsed[1])

    # 到期提醒：[dash] remind=false 关闭；remind_within=提前多少分钟提醒（默认 60）
    _dash_cfg = cfg_data.get("dash") or {}
    state["remind"] = str(_dash_cfg.get("remind", "true")).lower() not in ("false", "0", "no")
    try:
        state["remind_within"] = max(5, int(_dash_cfg.get("remind_within", 60)))
    except (TypeError, ValueError):
        state["remind_within"] = 60
    state["reminded"] = set()          # 本次运行已提醒过的 todo_id，不重复打扰

    def switch_tab(key: str) -> None:
        if state["tab"] == key:
            return
        state["tab"] = key
        state["offset"] = 0
        render()

    # ---- 尺寸与列表 ----
    def _set_size(w: int, h: int) -> None:
        """直接到位：改窗口/画布尺寸并重画背景。

        位置从 geometry() 字符串解析（窗口未映射时 winfo_x/y 会返回 0，
        直接把窗口顶到 (0,0)——启动动画阶段就会踩到）。
        """
        import re as _re

        m = _re.search(r"([+-]\d+)([+-]\d+)$", root.geometry())
        pos = f"{m.group(1)}{m.group(2)}" if m else "+120+120"
        root.geometry(f"{w}x{h}{pos}")
        cv.config(width=w, height=h)
        _draw_bg(cv, w, h)

    def _resize(h: int, w: int = W) -> None:
        if h == state["h"] and w == state["w"]:
            return
        old_h, old_w = state.get("h", h), state.get("w", w)
        state["h"], state["w"] = h, w
        state["anim_gen"] = state.get("anim_gen", 0) + 1
        gen = state["anim_gen"]
        # 变化太小或动画关闭时直接到位；否则约 220ms 缓动过去（对齐 DeskBox Smooth 档）
        if not ANIM_ON or (abs(h - old_h) < 24 and abs(w - old_w) < 24):
            _set_size(w, h)
            return
        frames = 14

        def _step(i: int) -> None:
            if state.get("anim_gen") != gen:
                return                       # 被更新的尺寸打断
            t = _ease_out_cubic(i / frames)
            _set_size(round(old_w + (w - old_w) * t),
                      round(old_h + (h - old_h) * t))
            if i < frames:
                root.after(16, lambda: _step(i + 1))

        _step(1)

    state["w"] = W

    # ---- 固定元素：时钟 / 日期 ----
    clock = cv.create_text(W // 2, 66, text="00:00:00", fill=TEXT,
                           font=("Consolas", 30, "bold"), tags="clock")
    date_id = cv.create_text(W // 2, 104, text="", fill=SUB,
                             font=("Microsoft YaHei UI", 9), tags="clock")

    def tick() -> None:
        global W

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
        today = time.strftime("%Y-%m-%d")
        if state.get("day") and state["day"] != today:
            state["day"] = today
            housekeeping()          # 第二天上班：把昨天完成的条目归档
            render()
        elif not state.get("day"):
            state["day"] = today

        if state.pop("reveal", False):
            reveal()
        # 配置改了（换色/改透明度）就热重载，不用重启看板
        mtime = _config_mtime()
        if mtime and mtime != state.get("cfg_mtime"):
            first = state.get("cfg_mtime") is None
            state["cfg_mtime"] = mtime
            if not first:
                ui = _apply_ui_config()
                W = state.get("w", W)    # 热重载不覆盖用户拖出来的宽度
                try:
                    root.attributes("-alpha", ui["alpha"])
                except Exception:  # noqa: BLE001
                    pass
                state["alpha"] = ui["alpha"]
                state["fade_gen"] = state.get("fade_gen", 0) + 1   # 打断进行中的淡入
                _draw_bg(cv, W, state.get("h", H))
                _apply_blur(root, BLUR_ON)
                render()
        # 唤出状态到期 → 收起；平时贴桌面层，被点浮上来也会被压回去
        if state.get("reveal_on") and time.time() >= state.get("reveal_until", 0):
            state["reveal_on"] = False
            _sink_fade()            # 先淡出再沉回（动画关时 tick 末尾直接压层）
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
            try:
                import os as _os

                mtime = _os.stat(db_path).st_mtime
            except Exception:  # noqa: BLE001
                mtime = 0
            cached = state.get("cached_items")
            if cached is not None and mtime == state.get("db_mtime"):
                todo_items = cached        # 库没变就复用缓存，省一次 sqlite 连接
            else:
                try:
                    todo_items = load_items(db_path, "pending")
                except Exception as exc:  # noqa: BLE001 - 界面不能因为读库失败就崩
                    todo_items = []
                    err = str(exc)
                else:
                    err = ""
                    state["cached_items"] = todo_items
                    state["db_mtime"] = mtime

            # render debounce：tab/query/条目全没变就整跳过（不 cv.delete 不重画）
            signatures = tuple(
                (i.todo_id, i.status, (i.text or "")[:40], bool(i.done_at))
                for i in todo_items)
            debounce = (state["mini"], state["tab"], state.get("query", ""),
                        state.get("batch", False), state.get("pin", False),
                        state.get("search", False), mtime, signatures)
            if debounce == state.get("render_hash"):
                return                       # 什么都没变，省一次全量重画
            state["render_hash"] = debounce

            cv.delete("body")
            unconfirmed = sum(1 for i in todo_items if i.status == "unconfirmed")

            if state["mini"]:
                _render_mini(len(todo_items), unconfirmed)
                return
            _render_full(todo_items, unconfirmed, err)
            # 每行 hover 高亮：<Enter> 改 fill 为稍亮色、<Leave> 恢复；
            # 因为 cv.delete("body") 会清掉旧绑定，所以每次 render 后重绑
            HOVER_FILL = "#3a363d"      # Hover 时的微亮色（比 FIELD #2c2b31 亮一点）
            for item in state.get("visible", []):
                tag = f"row-{item.todo_id}"
                picked = item.todo_id in state["sel"]
                base_fill = ACTIVE if picked else FIELD

                def _on_enter(_e, t=tag):
                    cv.itemconfig(t, fill=HOVER_FILL)

                def _on_leave(_e, t=tag, f=base_fill):
                    cv.itemconfig(t, fill=f)

                cv.tag_bind(tag, "<Enter>", _on_enter)
                cv.tag_bind(tag, "<Leave>", _on_leave)
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
        if sentry.winfo_ismapped():
            sentry.place_forget()

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

        tab_len = len(items)             # 搜索前的标签页数量，给分段控件计数用
        query = (state.get("query") or "").strip()
        if query:
            items = filter_items(items, query)

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
        cv.create_text(W - 152, 24, text=time.strftime("%H:%M"), anchor="e", fill=SUB,
                       font=("Consolas", 10), tags=("body", "hclock"))
        if not SHOW_CLOCK:
            cv.itemconfig("hclock", state="hidden")
        cv.create_text(W - 122, 24, text="☑", anchor="e",
                       fill=(ACCENT if state["batch"] else SUB),
                       font=("Segoe UI Symbol", 11), tags=("body", "multi"))
        cv.create_text(W - 20, 24, text="✕", anchor="e", fill=SUB,
                       font=("Microsoft YaHei UI", 11), tags=("body", "close"))
        cv.create_text(W - 44, 24, text="—", anchor="e", fill=SUB,
                       font=("Microsoft YaHei UI", 11), tags=("body", "mini"))
        pinned = bool(state.get("pin"))
        cv.create_text(W - 68, 24, text="钉", anchor="e", fill=ACCENT if pinned else SUB,
                       font=("Microsoft YaHei UI", 9), tags=("body", "pin"))
        cv.create_text(W - 94, 24, text="⚙", anchor="e", fill=SUB,
                       font=("Segoe UI Symbol", 11), tags=("body", "gear"))
        cv.tag_bind("gear", "<Button-1>", lambda _e: open_settings())
        cv.tag_bind("multi", "<Button-1>", lambda _e: toggle_batch())

        # 顶部输入卡：平时是「＋ 添加待办」，批量模式下变成操作条
        _rounded(cv, PAD, 48, W - PAD, 88, 12, fill=FIELD, outline=BORDER, width=1, tags="body")
        if state["batch"]:
            n_sel = len(state["sel"])
            cv.create_text(PAD + 14, 68, text=f"已选 {n_sel}", anchor="w", fill=TEXT,
                           font=("Microsoft YaHei UI", 9, "bold"), tags="body")
            selected_all = bool(items) and len(state["sel"]) >= len([i for i in items])
            cv.create_text(PAD + 96, 68, text=("全不选" if selected_all else "全选"),
                           anchor="w", fill=ACCENT,
                           font=("Microsoft YaHei UI", 8), tags=("body", "ball"))
            cv.tag_bind("ball", "<Button-1>", lambda _e: batch_select_all())
            bx = W - PAD - 12
            for label, tag, fn in (("取消", "bcancel", None),
                                   ("删除", "bdel", None),
                                   ("完成", "bdone", None)):
                wlab = 44 if label != "取消" else 40
                x1 = bx - wlab
                fill = ACCENT if tag == "bdone" else "none"
                fg = "#0b1a22" if tag == "bdone" else SUB
                if fill != "none":
                    _rounded(cv, x1, 57, bx, 79, 8, fill=fill, outline="", tags=("body", tag))
                cv.create_text((x1 + bx) / 2, 68, text=label, anchor="center", fill=fg,
                               font=("Microsoft YaHei UI", 8), tags=("body", tag))
                cv.tag_bind(tag, "<Button-1>",
                            lambda _e, t=tag: batch_action(t))
                bx = x1 - 8
            cv.create_text(PAD + 14, 68, text=f"已选 {n_sel}", anchor="w", fill=TEXT,
                           font=("Microsoft YaHei UI", 9, "bold"), tags="body")
        else:
            searching = bool(state.get("search"))
            cv.create_text(PAD + 18, 68, text=("搜" if searching else "＋"),
                           anchor="center", fill=(ACCENT if searching else SUB),
                           font=("Microsoft YaHei UI", 9 if searching else 12), tags="body")

        # 筛选标签：圆角胶囊 + 数量（DeskBox 的 All / Today / ... 风格）
        open_count = sum(1 for i in todo_items if i.status != "done")
        counts = {
            "todo": tab_len,
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
        state["visible"] = visible

        y = TOP + 14
        if err:
            cv.create_text(PAD, y, text=f"读取失败：{err}", anchor="w", fill="#ff8fab",
                           width=W - 2 * PAD, tags="body")
        elif not items:
            if query:
                empty = f"没有匹配「{query}」的条目"
                hint = "换个关键词，或按 Esc 退出搜索"
            elif state["tab"] == "todo":
                # todo 标签页完全空：要么第一次用、要么 sync 没跑过/没抽到
                import os as _os

                if not _os.path.exists(db_path):
                    empty = "第一次使用 Chat2Todo"
                    hint = "按 Ctrl+Shift+C 打开控制台\n→ 填 QQ 口令\n→ 点「立即同步」"
                    cv.create_text(W / 2, y + 10, text="👋", anchor="center",
                                   fill=SUB, font=("Segoe UI Emoji", 28), tags="body")
                    cv.create_text(W / 2, y + 54, text=empty, anchor="center",
                                   fill=TEXT, font=("Microsoft YaHei UI", 11, "bold"),
                                   tags="body")
                    cv.create_text(W / 2, y + 80, text=hint, anchor="center",
                                   fill=SUB, width=W - 2 * PAD,
                                   font=("Microsoft YaHei UI", 9), tags="body")
                elif not todo_items:
                    # db 有 messages 但 todos 为空（还没 sync 过）
                    empty = "还没有待办"
                    hint = "点右下角 ⚙ 打开控制台 → 点「立即同步」\n同步完最多 5 秒自动出现"
                    cv.create_text(W / 2, y + 20, text=empty, anchor="center",
                                   fill=TEXT, font=("Microsoft YaHei UI", 10), tags="body")
                    cv.create_text(W / 2, y + 54, text=hint, anchor="center",
                                   fill=SUB, width=W - 2 * PAD,
                                   font=("Microsoft YaHei UI", 8), tags="body")
                else:
                    # 有 todos 但被批量模式/offset 过滤完了
                    empty = "没有待办"
                    hint = "下方加一个，或等同步捞进来"
                    cv.create_text(W / 2, y + 30, text=empty, anchor="center",
                                   fill=TEXT, font=("Microsoft YaHei UI", 10), tags="body")
                    cv.create_text(W / 2, y + 54, text=hint, anchor="center",
                                   fill=SUB, width=W - 2 * PAD,
                                   font=("Microsoft YaHei UI", 8), tags="body")
            else:
                empty = {"star": "还没有重点条目",
                         "maybe": "没有待确认的条目",
                         "today": "今天没有到期的待办",
                         "done": "还没有完成的记录"}[state["tab"]]
                hint = {"star": "在控制台把某人/某个群设为「特别关心」即可",
                        "maybe": "规则拿不准的条目会先放这里，点一下确认",
                        "today": "今天没有截止项，轻松一点",
                        "done": "完成一条就会出现在这里"}[state["tab"]]
                cv.create_text(W / 2, y + 30, text=empty, anchor="center", fill=TEXT,
                               font=("Microsoft YaHei UI", 10), tags="body")
                cv.create_text(W / 2, y + 54, text=hint, anchor="center", fill=SUB,
                               width=W - 2 * PAD, font=("Microsoft YaHei UI", 8), tags="body")
        else:
            from ..core.deadline import label as due_label, parse as due_parse

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
                picked = item.todo_id in state["sel"]
                row_tag = f"row-{item.todo_id}"
                _rounded(cv, PAD, y - 26, W - PAD, y + 26, min(14, RADIUS),
                         fill=(ACTIVE if picked else FIELD),
                         outline=(ACCENT if picked else BORDER), width=(2 if picked else 1),
                         tags=("body", row_tag))
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
                base_c = SUB if item.status != "pending" else TEXT
                use_font = FONT_ITEM_DONE if item.status == "done" else FONT_ITEM
                # 搜索模式下，把正文里命中关键词那段用强调色高亮
                q = (state.get("query") or "").strip()
                x_t, y_t = PAD + 48, y - 9
                if q:
                    lower = body.lower()
                    idx = lower.find(q.lower())
                    if idx >= 0:
                        before = body[:idx]
                        hit = body[idx:idx + len(q)]
                        after = body[idx + len(q):]
                        cur = x_t
                        for seg, color in ((before, base_c), (hit, ACCENT), (after, base_c)):
                            if not seg:
                                continue
                            cv.create_text(cur, y_t, text=seg, anchor="w", fill=color,
                                           font=use_font, tags=("body", tag))
                            cur += use_font.measure(seg)
                    else:
                        cv.create_text(x_t, y_t, text=body, anchor="w", fill=base_c,
                                       font=use_font, tags=("body", tag))
                else:
                    cv.create_text(x_t, y_t, text=body, anchor="w", fill=base_c,
                                   font=use_font, tags=("body", tag))
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
                cv.create_text(W - PAD - 34, y - 8, text="⋯", anchor="e", fill="#6f6a64",
                               font=("Microsoft YaHei UI", 9), tags=("body", f"more{idx}"))
                # 行尾最右：修改按钮（点一下弹小窗改文字）
                cv.create_text(W - PAD - 12, y - 8, text="改", anchor="e", fill="#6f6a64",
                               font=("Microsoft YaHei UI", 8), tags=("body", f"edit{idx}"))

                if state["batch"]:
                    cv.tag_bind(tag, "<Button-1>", lambda _e, it=item: toggle_sel(it))
                    cv.tag_bind(f"dot{idx}", "<Button-1>", lambda _e, it=item: toggle_sel(it))
                else:
                    # 双击定位：提到 QQ 下载文件的直接选中文件，否则看原文出处
                    cv.tag_bind(tag, "<Double-Button-1>",
                                lambda _e, it=item: locate_source(it))
                    cv.tag_bind(f"dot{idx}", "<Button-1>",
                                lambda _e, it=item: toggle_item(it))
                cv.tag_bind(f"more{idx}", "<Button-1>",
                            lambda e, it=item: popup(e, it))
                cv.tag_bind(f"edit{idx}", "<Button-1>",
                            lambda _e, it=item: edit_item(it))


                for t in (tag, f"dot{idx}", f"more{idx}", f"edit{idx}"):
                    cv.tag_bind(t, "<Button-3>", lambda e, it=item: popup(e, it))
                y += ROW

        # 页脚：左边统计，右边「清除已完成」胶囊按钮
        # 底部折叠堆叠提示：还有条目没显示时，画两层逐渐变窄的卡片边
        rows_room = max(1, (h - TOP - FOOT) // ROW)
        if len(items) > state["offset"] + rows_room:
            rest = len(items) - state["offset"] - rows_room
            for k, inset in enumerate((26, 13)):
                yy = h - 92 - k * 7
                _rounded(cv, PAD + inset, yy, W - PAD - inset, yy + 10, 6,
                         fill=FIELD, outline=BORDER, width=1, tags="body")
            cv.create_text(W / 2, h - 106, text=f"还有 {rest} 条 · 滚轮继续",
                           anchor="center", fill=SUB,
                           font=("Microsoft YaHei UI", 7), tags="body")

        foot_y = h - 26
        cv.create_text(PAD, foot_y, text=f"待办 {open_count} · 已完成 {max(0, len(items) - open_count)}",
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

        # 输入框压在顶部卡片上：搜索模式放搜索框，批量模式两个都藏起来
        if state["batch"]:
            entry.place_forget()
            sentry.place_forget()
        elif state.get("search"):
            entry.place_forget()
            sentry.place(x=PAD + 42, y=57, width=W - 2 * PAD - 52, height=22)
        else:
            sentry.place_forget()
            entry.place(x=PAD + 42, y=57, width=W - 2 * PAD - 52, height=22)

    # ---- 交互绑定 ----
    cv.tag_bind("close", "<Button-1>", lambda _e: close())
    cv.tag_bind("mini", "<Button-1>", lambda _e: toggle_mini())
    cv.tag_bind("pin", "<Button-1>", lambda _e: toggle_pin())

    cv.bind("<Button-1>", on_press)
    cv.bind("<B1-Motion>", on_move)

    def on_release(_e) -> None:
        mode = drag.get("mode", "move")
        if mode in ("resize-l", "resize-r"):
            _persist()                   # 宽度一并记住
            render()                     # 用新宽度重排条目
            return
        # 迷你模式下轻点任意处展开（拖动不算）
        if state["mini"] and not drag["moved"]:
            current = cv.find_withtag("current")
            if "close" in cv.gettags(current):
                return
            toggle_mini()
            return
        if mode == "move" and drag["moved"]:
            gx, gy = _geom_pos()
            if SNAP_ON:                  # 松手时贴边（对齐 DeskBox 边缘吸附）
                nx, ny = _snap_to_edges(gx, gy, state.get("w", W),
                                        state.get("h", H),
                                        _monitor_work_areas(), SNAP_PX)
                if (nx, ny) != (gx, gy):
                    root.geometry(f"+{nx}+{ny}")
            _persist()

    cv.bind("<ButtonRelease-1>", on_release)
    cv.bind("<Motion>", on_hover)
    cv.bind("<Enter>", on_enter)
    cv.bind("<Leave>", on_leave)
    cv.bind("<Button-3>", popup_win)

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
        state["alpha"] = round(nxt, 2)              # 淡入动画的新终点
        state["fade_gen"] = state.get("fade_gen", 0) + 1
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
        if state.get("search"):
            search_close()               # Ctrl+N：从搜索切回「添加待办」
        ph_off()
        root.after(60, entry.focus_set)
        return "break"        # Ctrl+N：收起时先展开，再把光标放进输入框

    entry.bind("<Return>", submit)
    entry.bind("<Escape>", _blur)
    sentry.bind("<KeyRelease>", search_type)
    sentry.bind("<Escape>", search_close)
    sentry.bind("<Return>", lambda _e: "break")

    root.bind("<Escape>", lambda _e: close())
    root.bind("<Control-z>", lambda _e: undo())
    root.bind("<Control-n>", _new_todo)
    root.bind("<Control-f>", lambda _e: (search_open(), "break")[1])

    tick()
    render()
    # 固定 5 秒轮询：render debounce 兜底——tab/query/条目全没变就整跳过，
    # mtime 没变时 CPU 几乎零开销；同步完新待办最多 5s 就能看到
    refresh_ms = 5000

    def show_remind_toast(pairs) -> None:
        """右下角弹一张到期提醒小卡片：点击唤出看板，8 秒自动消失。"""
        try:
            from ..core.deadline import label as _due_label

            toast = tk.Toplevel(root)
            toast.overrideredirect(True)
            toast.attributes("-topmost", True)
            try:
                toast.attributes("-alpha", 0.96)
            except tk.TclError:
                pass
            body = tk.Frame(toast, bg=PANEL, highlightbackground=BORDER,
                            highlightthickness=1)
            body.pack(fill="both", expand=True)
            tk.Label(body, text="⏰ 到期提醒", bg=PANEL, fg=ACCENT, anchor="w",
                     font=("Microsoft YaHei UI", 9, "bold")).pack(
                fill="x", padx=12, pady=(8, 2))
            for item, due in pairs[:3]:
                txt = item.text if len(item.text) <= 22 else item.text[:22] + "…"
                tk.Label(body, text=f"{_due_label(due)}　{txt}", bg=PANEL, fg=TEXT,
                         anchor="w", font=("Microsoft YaHei UI", 8)).pack(
                    fill="x", padx=12)
            if len(pairs) > 3:
                tk.Label(body, text=f"还有 {len(pairs) - 3} 条…", bg=PANEL, fg=SUB,
                         anchor="w", font=("Microsoft YaHei UI", 8)).pack(
                    fill="x", padx=12)
            tk.Label(body, text="点击查看看板", bg=PANEL, fg=SUB, anchor="w",
                     font=("Microsoft YaHei UI", 7)).pack(fill="x", padx=12,
                                                          pady=(2, 8))
            toast.update_idletasks()
            tw, th = toast.winfo_reqwidth(), toast.winfo_reqheight()
            rects = _monitor_work_areas()
            if rects:
                _l, _t, r, b = rects[0]
            else:
                r, b = root.winfo_screenwidth(), root.winfo_screenheight()
            toast.geometry(f"+{r - tw - 16}+{b - th - 16}")

            def _dismiss(_evt=None) -> None:
                try:
                    toast.destroy()
                except Exception:  # noqa: BLE001
                    pass
                reveal()

            for w_ in [toast, body] + list(body.winfo_children()):
                w_.bind("<Button-1>", _dismiss)

            def _auto_close() -> None:
                try:
                    if toast.winfo_exists():
                        toast.destroy()
                except Exception:  # noqa: BLE001
                    pass

            toast.after(8000, _auto_close)
        except Exception:  # noqa: BLE001
            pass

    def check_reminders() -> None:
        """每轮刷新顺手查一次：有快到期且本次运行还没提醒过的条目就弹卡片。"""
        if not state.get("remind"):
            return
        try:
            pairs = [(i, d) for i, d in due_remindable(
                load_items(db_path, "pending"),
                within=int(state.get("remind_within", 60)) * 60)
                if i.todo_id not in state["reminded"]]
            if not pairs:
                return
            state["reminded"].update(i.todo_id for i, _ in pairs)
            show_remind_toast(pairs)
        except Exception:  # noqa: BLE001
            pass

    def loop() -> None:
        check_reminders()
        render()
        root.after(refresh_ms, loop)

    root.after(refresh_ms, loop)
    return root, cv, state, refresh_ms
