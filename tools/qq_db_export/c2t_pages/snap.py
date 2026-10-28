"""页：截图识别（视觉模型、AI 整理开关、截图热键）。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


def build(app, ctx) -> None:
    p = app._pal()
    page = app._new_page()
    app._pages["snap"] = page

    r = app._card_row(
        p, page, "📷", "视觉模型",
        "留空=只用系统 OCR（离线零安装）；填视觉模型=看图直提，如 qwen2.5vl:7b")
    app.vision = ttk.Combobox(r, values=["（不使用）", ctx.read_snap_model() or ""],
                              width=24, font=("Microsoft YaHei UI", 9))
    app.vision.pack(side="left")
    app.vision.set(ctx.read_snap_model() or "（不使用）")

    r = app._card_row(p, page, "✨", "AI 整理识别文字",
                      "识别后交给本地模型纠错、去噪、分行；关闭则原样显示 OCR 结果")
    refine_on = {"on": str(ctx.read_snap_refine()).lower() != "false"}
    mk = tk.Canvas(r, width=46, height=24, bg=p["card"], highlightthickness=0,
                   cursor="hand2")
    mk.pack(side="left")

    def draw_refine() -> None:
        mk.delete("all")
        on = refine_on["on"]
        track = p["accent"] if on else ("#c7ccd6" if app.theme == "light" else "#3a4553")
        mk.create_oval(2, 4, 22, 24, fill=track, outline="")
        mk.create_oval(24, 4, 44, 24, fill=track, outline="")
        mk.create_rectangle(12, 4, 34, 24, fill=track, outline="")
        x = 32 if on else 12
        mk.create_oval(x - 9, 3, x + 9, 21, fill="#ffffff", outline="")

    def flip_refine(_e=None) -> None:
        refine_on["on"] = not refine_on["on"]
        draw_refine()
        ctx.write_section("snap", {"ai_refine": refine_on["on"]})
        app.write(f"AI 整理已{'开启' if refine_on['on'] else '关闭'}（下次截图生效）\n")

    mk.bind("<Button-1>", flip_refine)
    draw_refine()

    r = app._card_row(p, page, "⌨", "截图热键",
                      "按热键后拖动框选屏幕区域；点「录制」可改键")
    app.snap_hotkey_label = tk.Label(r, text=ctx.read_hotkey("snap", "hotkey", "ctrl+alt+s"),
                                     bg=p["card"], fg=p["text"], font=("Consolas", 9))
    app.snap_hotkey_label.pack(side="left", padx=(0, 10))
    tk.Button(r, text="录制",
              command=lambda: app._record_hotkey("snap", "hotkey",
                                                 app.snap_hotkey_label, "ctrl+alt+s"),
              bg=p["btn"], fg=p["btn_text"], bd=0, padx=12, pady=4,
              cursor="hand2", font=("Microsoft YaHei UI", 8)).pack(side="left")
