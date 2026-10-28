"""页：AI 服务（服务商、端点、密钥与连接测试）。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


def build(app, ctx) -> None:
    p = app._pal()
    page = app._new_page()
    app._pages["ai"] = page
    cfg = ctx.read_cfg()
    ai_ok = str(cfg.get("enabled")).lower() == "true"
    local_now = str(cfg.get("base_url", "")).startswith(("http://127.0.0.1", "http://localhost"))

    r = app._card_row(
        p, page, "✨", "服务状态",
        f"已启用 · {'本地 Ollama' if local_now else '云端'} · {cfg.get('model', '')}"
        if ai_ok else "未启用（仅用规则抽取，不联网）")
    tk.Label(r, text="● 运行中" if ai_ok else "○ 未启用", bg=p["card"],
             fg=p["ok"] if ai_ok else p["muted"],
             font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")

    r = app._card_row(p, page, "🏷", "服务商", "选择后自动填入地址与模型")
    preset = ttk.Combobox(r, values=list(ctx.ai_presets), state="readonly", width=22,
                          font=("Microsoft YaHei UI", 9))
    preset.pack(side="left")
    preset.set("本地 Ollama（免费无需 Key）")
    app._preset = preset

    r = app._card_row(p, page, "🌐", "API 地址", "OpenAI 兼容端点")
    app.base_entry = tk.Entry(r, width=30, bg=p["field"], fg=p["field_text"],
                              insertbackground=p["field_text"], bd=0, relief="flat",
                              highlightthickness=1, highlightbackground=p["border"],
                              font=("Microsoft YaHei UI", 9))
    app.base_entry.pack(side="left", ipady=4)
    app.base_entry.insert(0, cfg["base_url"])

    r = app._card_row(p, page, "🧠", "模型", "本地建议 qwen2.5:14b")
    app.model_entry = tk.Entry(r, width=26, bg=p["field"], fg=p["field_text"],
                               insertbackground=p["field_text"], bd=0, relief="flat",
                               highlightthickness=1, highlightbackground=p["border"],
                               font=("Microsoft YaHei UI", 9))
    app.model_entry.pack(side="left", ipady=4)
    app.model_entry.insert(0, cfg["model"])

    r = app._card_row(p, page, "🔐", "API Key", "本地 Ollama 可留空（自动用占位值）")
    app.key_entry = tk.Entry(r, width=30, show="*", bg=p["field"], fg=p["field_text"],
                             insertbackground=p["field_text"], bd=0, relief="flat",
                             highlightthickness=1, highlightbackground=p["border"],
                             font=("Microsoft YaHei UI", 9))
    app.key_entry.pack(side="left", ipady=4)
    if cfg["api_key"] and cfg["api_key"] != "ollama":
        app.key_entry.insert(0, cfg["api_key"])

    r = app._card_row(p, page, "🔌", "连接测试", "会向服务端发一次最小请求")
    app.probe_btn = tk.Button(r, text="测试连接", bg=p["accent"], fg=p["accent_fg"],
                              bd=0, padx=14, pady=4, cursor="hand2",
                              font=("Microsoft YaHei UI", 9), command=app._do_probe)
    app.probe_btn.pack(side="left")
    app.hint = tk.Label(r, text="", bg=p["card"], fg=p["muted"],
                        font=("Microsoft YaHei UI", 8))
    app.hint.pack(side="left", padx=(10, 0))

    def on_preset(_evt=None) -> None:
        base, model = ctx.ai_presets.get(app._preset.get(), ("", ""))
        if not base:
            return
        app.base_entry.delete(0, "end")
        app.base_entry.insert(0, base)
        app.model_entry.delete(0, "end")
        app.model_entry.insert(0, model)

    preset.bind("<<ComboboxSelected>>", on_preset)
