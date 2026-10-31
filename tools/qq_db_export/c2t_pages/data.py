"""页：数据（原文检索、导出、统计、自检、清理与插件）。"""

from __future__ import annotations

import subprocess
import sys
import tkinter as tk
from tkinter import filedialog


def build(app, ctx) -> None:
    p = app._pal()
    page = app._new_page()
    app._pages["data"] = page

    db = str(ctx.db)
    # 这几个子命令没有 --db 参数，硬加会被 argparse 拒绝
    NO_DB = {"adapters", "plugins", "db", "db-key"}

    def run(*args: str) -> None:
        """把一条 CLI 子命令接到运行日志里跑。"""
        cmd = [sys.executable, "-m", "todoseine", *args]
        if args and args[0] not in NO_DB and "--db" not in args:
            cmd += ["--db", db]
        app._stream("数据", cmd)

    # ---- 原文检索 ----
    r = app._card_row(p, page, "⌕", "检索原文", "全文索引命中的原始消息，找回待办的出处")
    kw = tk.Entry(r, width=26, bg=p["field"], fg=p["text"], bd=0, relief="flat",
                  insertbackground=p["text"], font=("Microsoft YaHei UI", 9))
    kw.pack(side="left", padx=(0, 8), ipady=4)

    def do_search(_e=None) -> None:
        text = kw.get().strip()
        if not text:
            return
        run("search", text, "--limit", "30")

    kw.bind("<Return>", do_search)
    app._pill(p, r, "检索", do_search, primary=True)

    # ---- 上下文 ----
    r = app._card_row(p, page, "☰", "查看上下文", "填消息编号，看这一条前后的原文")
    mid = tk.Entry(r, width=10, bg=p["field"], fg=p["text"], bd=0, relief="flat",
                   insertbackground=p["text"], font=("Consolas", 9))
    mid.pack(side="left", padx=(0, 8), ipady=4)

    def do_show(_e=None) -> None:
        text = mid.get().strip()
        if not text.isdigit():
            return
        run("show", text)

    mid.bind("<Return>", do_show)
    app._pill(p, r, "查看", do_show)

    # ---- 导出 ----
    r = app._card_row(p, page, "⤓", "导出待办", "md / csv / json 直接写到 out 目录；ChatLab 需选路径")

    def do_export(fmt: str) -> None:
        run("export", "--format", fmt)

    app._pill(p, r, "Markdown", lambda: do_export("md"), primary=True)
    app._pill(p, r, "CSV", lambda: do_export("csv"))
    app._pill(p, r, "JSON", lambda: do_export("json"))

    def do_chatlab() -> None:
        path = filedialog.asksaveasfilename(
            title="导出 ChatLab Format", defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("JSONL", "*.jsonl")])
        if path:
            run("export-chatlab", path)

    app._pill(p, r, "ChatLab", do_chatlab)

    # ---- 统计与自检 ----
    r = app._card_row(p, page, "📈", "统计 / 自检", "消息与待办计数、环境依赖检查、适配器与插件清单")
    app._pill(p, r, "统计", lambda: run("stats"), primary=True)
    app._pill(p, r, "7 天统计", lambda: run("stats", "--since", "7d"))
    app._pill(p, r, "自检", lambda: run("doctor"))
    app._pill(p, r, "适配器", lambda: run("adapters"))
    app._pill(p, r, "插件", lambda: run("plugins"))

    # ---- 数据库通道 ----
    r = app._card_row(p, page, "🗄", "数据库通道", "检查只读引擎、口令状态与能否成功试读")
    app._pill(p, r, "检查通道", lambda: run("db"), primary=True)
    app._pill(p, r, "口令状态", lambda: run("db-key", "--check"))

    # ---- 回填来源群 ----
    r = app._card_row(p, page, "↩", "回填来源群", "给历史消息补上群号，不跑抽取也不调用 AI")
    app._pill(p, r, "全部回填", lambda: run("db-backfill"), primary=True)
    app._pill(p, r, "近 30 天", lambda: run("db-backfill", "--days", "30"))

    # ---- 按群清理 ----
    r = app._card_row(p, page, "🧹", "按群清理",
                      "先「列出群」拿群号；预览只统计不删除，「清理」才真删")
    chat = tk.Entry(r, width=16, bg=p["field"], fg=p["text"], bd=0, relief="flat",
                    insertbackground=p["text"], font=("Consolas", 9))
    chat.pack(side="left", padx=(0, 8), ipady=4)
    app._pill(p, r, "列出群", lambda: run("purge-chat", "--list"))

    def do_purge(dry: bool, keep: bool) -> None:
        text = chat.get().strip()
        if not text:
            app.write("先填群号（点「列出群」可以查）\n")
            return
        args = ["purge-chat", "--chat", text]
        if dry:
            args.append("--dry-run")
        if keep:
            args.append("--keep-todos")
        run(*args)

    app._pill(p, r, "预览", lambda: do_purge(True, False))
    app._pill(p, r, "清理", lambda: do_purge(False, False))
    app._pill(p, r, "只删消息", lambda: do_purge(False, True))

    # ---- 本地 API ----
    r = app._card_row(p, page, "🌐", "本地 API",
                      "只监听 127.0.0.1:8765，不开放到局域网；点「停止服务」结束进程")
    srv = {"proc": None}

    def toggle_serve() -> None:
        proc = srv["proc"]
        if proc is not None and proc.poll() is None:
            proc.terminate()
            srv["proc"] = None
            btn_serve.config(text="启动服务")
            app.write("本地 API 已停止\n")
            return
        srv["proc"] = subprocess.Popen(
            [sys.executable, "-m", "todoseine", "serve", "--db", db],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        btn_serve.config(text="停止服务")
        app.write("本地 API 已启动：http://127.0.0.1:8765\n")

    btn_serve = app._pill(p, r, "启动服务", toggle_serve, primary=True)
