"""页：待办（列表、搜索、新增、完成、删除、判定报告）。"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import messagebox

STATUS_LABEL = {"pending": "待办", "unconfirmed": "未确认", "done": "已完成",
                "noise": "非待办", "muted": "已静音"}
FILTERS = [("all", "全部"), ("pending", "待办"), ("unconfirmed", "未确认"),
           ("done", "已完成"), ("muted", "已静音")]


def build(app, ctx) -> None:
    p = app._pal()
    page = app._new_page()
    app._pages["todo"] = page

    state = {"rows": [], "status": "all"}

    def _store():
        from todoseine.core import Store

        return Store(str(ctx.db))

    # ---- 新增 ----
    r = app._card_row(p, page, "＋", "新增待办", "手动补一条，直接进「待办」")
    new_ent = tk.Entry(r, width=30, bg=p["field"], fg=p["text"], bd=0, relief="flat",
                       insertbackground=p["text"], font=("Microsoft YaHei UI", 9))
    new_ent.pack(side="left", padx=(0, 8), ipady=4)

    def add(_e=None) -> None:
        text = new_ent.get().strip()
        if not text:
            return
        try:
            store = _store()
            try:
                store.add_todo(text)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001
            app.write(f"新增失败：{exc}\n")
            return
        new_ent.delete(0, "end")
        app.write(f"已新增：{text}\n")
        refresh()

    new_ent.bind("<Return>", add)
    app._pill(p, r, "添加", add, primary=True)

    # ---- 搜索 + 状态筛选 ----
    r = app._card_row(p, page, "⌕", "搜索 / 筛选", "按内容、发送人或判定依据过滤，点标签切换状态")
    search_ent = tk.Entry(r, width=24, bg=p["field"], fg=p["text"], bd=0, relief="flat",
                          insertbackground=p["text"], font=("Microsoft YaHei UI", 9))
    search_ent.pack(side="left", padx=(0, 8), ipady=4)
    search_ent.bind("<KeyRelease>", lambda _e: refresh())

    fbtns = {}

    def set_filter(key: str) -> None:
        state["status"] = key
        for k, b in fbtns.items():
            b.configure(bg=p["accent"] if k == key else p["btn"],
                        fg=p["accent_fg"] if k == key else p["btn_text"])
        refresh()

    for key, label in FILTERS:
        b = tk.Button(r, text=label, command=lambda k=key: set_filter(k), relief="flat",
                      cursor="hand2", bg=p["btn"], fg=p["btn_text"], padx=10, pady=4,
                      borderwidth=0, highlightthickness=0,
                      font=("Microsoft YaHei UI", 8))
        b.pack(side="left", padx=(4, 0))
        fbtns[key] = b

    # ---- 列表 ----
    outer = tk.Frame(page, bg=p["card"], highlightbackground=p["border"],
                     highlightthickness=1)
    outer.pack(fill="both", expand=True, pady=(0, 8))
    inner = tk.Frame(outer, bg=p["card"])
    inner.pack(fill="both", expand=True, padx=14, pady=12)

    head = tk.Frame(inner, bg=p["card"])
    head.pack(fill="x", pady=(0, 6))
    tk.Label(head, text="待办列表", bg=p["card"], fg=p["text"], anchor="w",
             font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
    summary = tk.Label(head, text="0 条", bg=p["card"], fg=p["muted"],
                       font=("Microsoft YaHei UI", 8))
    summary.pack(side="left", padx=(10, 0))

    box = tk.Listbox(inner, bg=p["card"], fg=p["text"], bd=0, highlightthickness=0,
                     activestyle="none", font=("Microsoft YaHei UI", 9),
                     selectbackground=p["accent"], selectforeground=p["accent_fg"])
    box.pack(fill="both", expand=True)

    def _selected():
        sel = box.curselection()
        if not sel:
            messagebox.showinfo("待办", "先在列表里点一条")
            return None
        return state["rows"][sel[0]]

    def _selected_id():
        item = _selected()
        return None if item is None else item.todo_id

    def refresh() -> None:
        kw = search_ent.get().strip().lower()
        status = state["status"]
        try:
            store = _store()
            try:
                items = store.list_todos(None if status == "all" else status)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001
            app.write(f"读取待办失败：{exc}\n")
            return
        if status == "all":
            # 全部 = 还没被处理过的；否决掉的和自己静音的都在各自筛选里
            items = [i for i in items if i.status not in ("noise", "muted")]
        if kw:
            items = [i for i in items
                     if kw in (i.text or "").lower()
                     or kw in (i.sender or "").lower()
                     or kw in (i.reason or "").lower()]
        state["rows"] = items
        box.delete(0, "end")
        for i in items:
            text = (i.text or "").replace("\n", " ")
            if len(text) > 60:
                text = text[:60] + "…"
            box.insert("end", f"{str(i.todo_id):>4} · "
                              f"{STATUS_LABEL.get(i.status, i.status)} · {text}")
        summary.config(text=f"{len(items)} 条")

    # ---- 操作 ----
    r = app._card_row(p, page, "✓", "操作", "对选中那条待办执行；删除会进「最近删除」，可随时还原")

    def do_done() -> None:
        tid = _selected_id()
        if tid is None:
            return
        try:
            store = _store()
            try:
                ok = store.set_status(tid, "done")
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001
            app.write(f"标记失败：{exc}\n")
            return
        app.write(f"#{tid} {'已标记完成' if ok else '状态未变'}\n")
        refresh()

    def do_delete() -> None:
        tid = _selected_id()
        if tid is None:
            return
        if not messagebox.askyesno("删除待办", f"把 #{tid} 移进回收站？"):
            return
        try:
            store = _store()
            try:
                ok = store.trash_item(tid)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001
            app.write(f"删除失败：{exc}\n")
            return
        app.write(f"#{tid} {'已移进回收站' if ok else '不存在'}\n")
        refresh()

    def do_explain() -> None:
        tid = _selected_id()
        if tid is None:
            return
        app._stream("判定报告",
                    [sys.executable, "-m", "todoseine", "explain", str(tid),
                     "--db", str(ctx.db)])

    def do_mute(scope: str = "kind") -> None:
        item = _selected()
        if item is None:
            return
        if scope == "source":
            desc, hint = "整个来源", "这个群/导出里的所有消息"
        elif scope == "sender":
            desc, hint = f"「{item.sender}」", f"{item.sender} 发的所有消息"
        else:
            desc, hint = "此类消息", f"与「{item.text[:20]}…」相似的消息"
        if not messagebox.askyesno("不再提醒", f"以后{hint}都不再生成提醒？"):
            return
        try:
            store = _store()
            try:
                if scope == "source":
                    res = store.mute(item.session, "")
                elif scope == "sender":
                    res = store.mute(item.session, item.sender)
                else:
                    res = store.mute(item.session, "", item.text)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001
            app.write(f"设置静音失败：{exc}\n")
            return
        app.write(f"已静音（{desc}）：规则 #{res['rule_id']}，"
                  f"收起 {res['muted_todos']} 条待办\n")
        refresh()

    def do_unmute() -> None:
        item = _selected()
        if item is None:
            return
        try:
            store = _store()
            try:
                restored = store.release(item.session, item.sender, item.text)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001
            app.write(f"恢复提醒失败：{exc}\n")
            return
        if not restored:
            app.write("这条不在任何静音规则里。\n")
        else:
            app.write(f"已恢复提醒，{restored} 条回到待办\n")
        refresh()

    # 操作按钮分两行——8 个 pill 左 pack 一排的话，窗口一窄最后几个会被裁掉
    # （Tk pack 不会自动换行）。这里用一个 wrapper 纵向堆两行。
    wrap = tk.Frame(r, bg=p["card"])
    wrap.pack(side="left", anchor="e")
    row1 = tk.Frame(wrap, bg=p["card"])
    row1.pack(side="top", anchor="e")
    row2 = tk.Frame(wrap, bg=p["card"])
    row2.pack(side="top", anchor="e", pady=(4, 0))

    app._pill(p, row1, "标记完成", do_done, primary=True)
    app._pill(p, row1, "此类不再提醒", lambda: do_mute("kind"))
    app._pill(p, row1, "此人不再提醒", lambda: do_mute("sender"))
    app._pill(p, row1, "整群静音", lambda: do_mute("source"))

    app._pill(p, row2, "恢复提醒", do_unmute)
    app._pill(p, row2, "删除", do_delete)
    app._pill(p, row2, "判定报告", do_explain)
    app._pill(p, row2, "刷新", refresh)

    box.bind("<Double-Button-1>", lambda _e: do_explain())
    refresh()
