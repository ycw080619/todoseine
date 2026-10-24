"""桌面待办看板（可选能力，tkinter 实现，仍属标准库）。

设计约束（与方案第五章一致）：
- 只读数据库，勾选时才把状态改回 done，不碰原文；
- 窗口置顶常显，但**不做任何键鼠模拟**，也不去抢占其它程序的输入焦点；
- 关闭窗口不影响命令行与数据；没有图形环境时会给出可读的错误而不是栈。
"""

from __future__ import annotations

import sys
from typing import List

from .core import Store
from .models import TodoItem


def load_items(db_path: str, status: str = "pending") -> List[TodoItem]:
    """给界面用的纯函数：读一批待办，方便无 GUI 环境下测试。"""
    store = Store(db_path)
    try:
        return store.list_todos(status=status)
    finally:
        store.close()


def run(db_path: str, interval: int = 30, status: str = "pending") -> int:
    """启动看板，阻塞直到用户关闭窗口。"""
    try:
        import tkinter as tk
    except ImportError:  # 罕见：Python 未带 Tk
        print("[E3001] 当前 Python 未包含 tkinter，无法打开看板", file=sys.stderr)
        print("  建议：改用 c2t todos / c2t export，或安装带 Tk 的 Python", file=sys.stderr)
        return 1

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"[E3001] 无法创建窗口：{exc}", file=sys.stderr)
        return 1

    root.title("Chat2Todo")
    root.geometry("380x460")
    root.minsize(300, 260)
    root.attributes("-topmost", True)  # 置顶，避免被其它窗口压住
    root.configure(bg="#fbfbfb")

    header = tk.Label(
        root, text="待办", anchor="w", bg="#fbfbfb",
        font=("Microsoft YaHei UI", 11, "bold"), fg="#1f3a5f",
    )
    header.pack(fill="x", padx=12, pady=(10, 2))

    body = tk.Frame(root, bg="#fbfbfb")
    body.pack(fill="both", expand=True, padx=8, pady=4)

    footer = tk.Label(
        root, text="", anchor="w", bg="#fbfbfb",
        font=("Microsoft YaHei UI", 8), fg="#6b7280",
    )
    footer.pack(fill="x", padx=12, pady=(0, 8))

    state = {"busy": False}

    def refresh() -> None:
        if state["busy"]:
            return
        state["busy"] = True
        try:
            for child in body.winfo_children():
                child.destroy()
            try:
                items = load_items(db_path, status)
            except Exception as exc:  # noqa: BLE001 - 界面不能因为读库失败就崩
                tk.Label(body, text=f"读取失败：{exc}", bg="#fbfbfb", fg="#b91c1c",
                         wraplength=340, justify="left").pack(anchor="w")
                footer.config(text="")
                return
            if not items:
                tk.Label(body, text="没有待处理的事项 🎉", bg="#fbfbfb", fg="#6b7280").pack(
                    anchor="w", pady=8
                )
            for item in items:
                row = tk.Frame(body, bg="#fbfbfb")
                row.pack(fill="x", pady=1)
                var = tk.BooleanVar(value=False)

                def on_toggle(todo_id: int = item.todo_id, v: tk.BooleanVar = var) -> None:
                    if not v.get():
                        return
                    store = Store(db_path)
                    try:
                        store.set_status(todo_id, "done")
                    finally:
                        store.close()
                    refresh()

                tk.Checkbutton(
                    row, variable=var, command=on_toggle, bg="#fbfbfb",
                    activebackground="#fbfbfb", highlightthickness=0, bd=0,
                ).pack(side="left", anchor="n")
                text = item.text if len(item.text) <= 46 else item.text[:46] + "…"
                tk.Label(
                    row, text=text, bg="#fbfbfb", anchor="w", justify="left",
                    wraplength=300, font=("Microsoft YaHei UI", 9),
                ).pack(side="left", fill="x", expand=True)
            footer.config(text=f"共 {len(items)} 条 · 每 {interval}s 自动刷新 · Esc 关闭")
        finally:
            state["busy"] = False
            root.after(max(5, interval) * 1000, refresh)

    root.bind("<Escape>", lambda _e: root.destroy())
    refresh()
    root.mainloop()
    return 0
