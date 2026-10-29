"""页：最近删除（回收站列表与保留天数）。"""

from __future__ import annotations

import tkinter as tk


def build(app, ctx) -> None:
    page = app._new_page()
    app._pages["trash"] = page
    p = app._pal()
    tk.Label(page, text="勾掉的待办第二天会进这里，可随时还原", bg=p["bg"],
             fg=p["muted"], anchor="w",
             font=("Microsoft YaHei UI", 8)).pack(fill="x", padx=28, pady=(0, 6))

    srow = tk.Frame(page, bg=p["bg"])
    srow.pack(fill="x", padx=28, pady=(0, 4))
    tk.Label(srow, text="搜索", bg=p["bg"], fg=p["muted"],
             font=("Microsoft YaHei UI", 8)).pack(side="left")
    app.trash_search = tk.Entry(srow, bg=p["field"], fg=p["text"], bd=0,
                                highlightthickness=0, insertbackground=p["text"],
                                font=("Microsoft YaHei UI", 9))
    app.trash_search.pack(side="left", fill="x", expand=True, padx=(6, 0))
    app.trash_search.bind("<KeyRelease>", lambda _e: app._refresh_trash())

    card = tk.Frame(page, bg=p["card"], highlightbackground=p["border"],
                    highlightthickness=1)
    card.pack(fill="both", expand=True, padx=28, pady=(8, 16))
    app.trash_box = tk.Listbox(card, bg=p["card"], fg=p["text"], bd=0,
                               highlightthickness=0, activestyle="none",
                               font=("Microsoft YaHei UI", 9))
    app.trash_box.pack(fill="both", expand=True, padx=12, pady=(10, 6))

    bar = tk.Frame(card, bg=p["card"])
    bar.pack(fill="x", padx=12, pady=(0, 12))
    tk.Button(bar, text="全选", command=app.on_trash_select_all, bg=p["btn"],
              fg=p["btn_text"], bd=0, padx=10, pady=5, cursor="hand2",
              font=("Microsoft YaHei UI", 8)).pack(side="left")
    tk.Button(bar, text="还原选中", command=app.on_restore_trash, bg=p["btn"],
              fg=p["btn_text"], bd=0, padx=12, pady=5, cursor="hand2",
              font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(8, 0))
    tk.Button(bar, text="清空回收站", command=app.on_clear_trash, bg=p["btn"],
              fg=p["btn_text"], bd=0, padx=12, pady=5, cursor="hand2",
              font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(8, 0))
    tk.Button(bar, text="保存", command=app.on_save_trash_days, bg=p["btn"],
              fg=p["btn_text"], bd=0, padx=10, pady=5, cursor="hand2",
              font=("Microsoft YaHei UI", 8)).pack(side="right")
    app.trash_days = tk.Entry(bar, width=5, bg=p["field"], fg=p["text"], bd=0,
                              highlightthickness=0,
                              font=("Consolas", 9), justify="center")
    app.trash_days.pack(side="right", padx=(0, 8))
    app.trash_days.insert(0, str(app._read_trash_days()))
    tk.Label(bar, text="保留天数（0=永久）", bg=p["card"], fg=p["muted"],
             font=("Microsoft YaHei UI", 8)).pack(side="right", padx=(0, 6))

    app._refresh_trash()
