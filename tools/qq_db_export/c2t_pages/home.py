"""页：控制台（导入、快捷操作、运行日志）。"""

from __future__ import annotations

import tkinter as tk


def build(app, ctx) -> None:
    p = app._pal()
    page = app._new_page()
    app._pages["console"] = page

    r = app._card_row(p, page, "▦", "导入文件",
                      "选一份导出的 txt / JSON → 规则抽取 → 本地 AI 复核")
    app.import_btn = app._pill(p, r, "导入文件", app.on_import, primary=True)

    r = app._card_row(p, page, "⟳", "快捷操作", "看板与截图待办")
    app._pill(p, r, "打开看板", app.on_dashboard)
    app._pill(p, r, "截图待办", app.on_snap)

    r = app._card_row(p, page, "⚙", "更多", "立即同步、截图热键、目录监视与定时同步")
    app.sync_btn = app._pill(p, r, "立即同步", app.on_sync)
    app.snap_btn = app._pill(p, r, "截图热键", app.on_snap_hotkey)
    app.watch_btn = app._pill(p, r, "开始监视", app.on_watch)
    app._pill(p, r, "定时同步", app.on_schedule)

    outer = tk.Frame(page, bg=p["card"], highlightbackground=p["border"],
                     highlightthickness=1)
    outer.pack(fill="both", expand=True, pady=(0, 8))
    inner = tk.Frame(outer, bg=p["card"])
    inner.pack(fill="both", expand=True, padx=14, pady=12)
    tk.Label(inner, text="运行日志", bg=p["card"], fg=p["text"], anchor="w",
             font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(0, 6))
    app.log = tk.Text(inner, bg=p["log_bg"], fg=p["log_fg"],
                      insertbackground=p["log_fg"], relief="flat",
                      font=("Consolas", 9), wrap="word", borderwidth=0,
                      padx=12, pady=10, height=8)
    app.log.pack(fill="both", expand=True)
    app.log.configure(state="disabled")
