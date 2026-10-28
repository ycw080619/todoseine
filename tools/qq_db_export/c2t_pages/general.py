"""页：常规（外观、自启、快捷键、数据库口令、过滤名单、定时同步）。"""

from __future__ import annotations

import subprocess
import threading
import tkinter as tk


def build(app, ctx) -> None:
    p = app._pal()
    page = app._new_page()
    app._pages["general"] = page

    r = app._card_row(p, page, "🎨", "深色模式", "开启后整个控制台与看板切换为深色")
    mk = tk.Canvas(r, width=46, height=24, bg=p["card"], highlightthickness=0,
                   cursor="hand2")
    mk.pack(side="left")
    state = {"on": app.theme == "dark"}

    def draw_switch() -> None:
        mk.delete("all")
        on = state["on"]
        track = p["accent"] if on else ("#c7ccd6" if app.theme == "light" else "#3a4553")
        mk.create_oval(2, 4, 22, 24, fill=track, outline="")
        mk.create_oval(24, 4, 44, 24, fill=track, outline="")
        mk.create_rectangle(12, 4, 34, 24, fill=track, outline="")
        x = 32 if on else 12
        mk.create_oval(x - 9, 3, x + 9, 21, fill="#ffffff", outline="")

    def flip(_e=None) -> None:
        state["on"] = not state["on"]
        draw_switch()
        t = "dark" if state["on"] else "light"
        ctx.write_section("ui", {"theme": t})
        app._show_page("general")
        app.set_theme(t)

    mk.bind("<Button-1>", flip)
    draw_switch()

    r = app._card_row(p, page, "🚀", "开机自启",
                      "登录 Windows 时自动拉起看板与截图热键")
    auto_on = {"on": ctx.autostart_on()}
    ask = tk.Canvas(r, width=46, height=24, bg=p["card"], highlightthickness=0,
                    cursor="hand2")
    ask.pack(side="right")

    def draw_auto() -> None:
        ask.delete("all")
        on = auto_on["on"]
        track = p["accent"] if on else ("#c7ccd6" if app.theme == "light" else "#3a4553")
        ask.create_oval(2, 4, 22, 24, fill=track, outline="")
        ask.create_oval(24, 4, 44, 24, fill=track, outline="")
        ask.create_rectangle(12, 4, 34, 24, fill=track, outline="")
        x = 32 if on else 12
        ask.create_oval(x - 9, 3, x + 9, 21, fill="#ffffff", outline="")

    def flip_auto(_e=None) -> None:
        auto_on["on"] = not auto_on["on"]
        draw_auto()
        ok, msg = ctx.set_autostart(auto_on["on"])
        app.write(msg + "\n")

    ask.bind("<Button-1>", flip_auto)
    draw_auto()

    r = app._card_row(p, page, "🎨", "外观",
                      "强调色、面板底色、宽度、圆角、描边与时间显示；改完看板会自动刷新")
    box = tk.Frame(r, bg=p["card"])
    box.pack(side="right")

    def _swatch_row(parent, label, colors, on_pick):
        row = tk.Frame(parent, bg=p["card"])
        row.pack(side="top", anchor="e", pady=2)
        tk.Label(row, text=label, bg=p["card"], fg=p["muted"],
                 font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(0, 4))
        for color in colors:
            dot = tk.Canvas(row, width=16, height=16, bg=p["card"],
                            highlightthickness=0, cursor="hand2")
            dot.pack(side="left", padx=2)
            dot.create_oval(1, 1, 15, 15, fill=color, outline="#8b8b95")
            dot.bind("<Button-1>", lambda _e, c=color: on_pick(c))

    _swatch_row(box, "强调色",
                ("#4cc2ff", "#7c9cff", "#ff8fab", "#6ee7a8", "#ffd93d"),
                lambda c: ctx.save_ui_accent(c, app))
    _swatch_row(box, "面板",
                ("#212024", "#17181c", "#2b2a33", "#4a3f5c", "#f8f8fb"),
                lambda c: ctx.save_ui_key("panel", c, app, "面板底色"))

    geo_row = tk.Frame(box, bg=p["card"])
    geo_row.pack(side="top", anchor="e", pady=2)
    tk.Label(geo_row, text="宽度", bg=p["card"], fg=p["muted"],
             font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(0, 4))
    app.width_entry = tk.Entry(geo_row, width=5, bg=p["field"], fg=p["text"], bd=0,
                               relief="flat", justify="center", font=("Consolas", 9))
    app.width_entry.insert(0, str(ctx.read_ui("width", "360")))
    app.width_entry.pack(side="left", padx=(0, 10))
    tk.Label(geo_row, text="圆角", bg=p["card"], fg=p["muted"],
             font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(0, 4))
    app.radius_entry = tk.Entry(geo_row, width=5, bg=p["field"], fg=p["text"], bd=0,
                                relief="flat", justify="center", font=("Consolas", 9))
    app.radius_entry.insert(0, str(ctx.read_ui("radius", "18")))
    app.radius_entry.pack(side="left")

    op_row = tk.Frame(box, bg=p["card"])
    op_row.pack(side="top", anchor="e", pady=2)
    tk.Label(op_row, text="不透明度", bg=p["card"], fg=p["muted"],
             font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(0, 4))
    app.op_entry = tk.Entry(op_row, width=6, bg=p["field"], fg=p["text"], bd=0,
                            relief="flat", justify="center",
                            font=("Consolas", 9))
    app.op_entry.insert(0, str(ctx.read_ui("opacity", "0.93")))
    app.op_entry.pack(side="left")

    tog_row = tk.Frame(box, bg=p["card"])
    tog_row.pack(side="top", anchor="e", pady=2)

    def _ui_switch(parent, label: str, cfg_key: str, on: bool) -> None:
        """小型开关：即时写回 [ui]，看板 2 秒内自动生效。"""
        tk.Label(parent, text=label, bg=p["card"], fg=p["muted"],
                 font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(0, 4))
        mk = tk.Canvas(parent, width=46, height=24, bg=p["card"],
                       highlightthickness=0, cursor="hand2")
        mk.pack(side="left")
        state = {"on": on}

        def draw() -> None:
            mk.delete("all")
            track = p["accent"] if state["on"] else (
                "#c7ccd6" if app.theme == "light" else "#3a4553")
            mk.create_oval(2, 4, 22, 24, fill=track, outline="")
            mk.create_oval(24, 4, 44, 24, fill=track, outline="")
            mk.create_rectangle(12, 4, 34, 24, fill=track, outline="")
            x = 32 if state["on"] else 12
            mk.create_oval(x - 9, 3, x + 9, 21, fill="#ffffff", outline="")

        def flip(_e=None) -> None:
            state["on"] = not state["on"]
            draw()
            ctx.write_section("ui", {cfg_key: state["on"]})
            app.write(f"{label}已{'开启' if state['on'] else '关闭'}"
                      "（看板会自动刷新）\n")

        mk.bind("<Button-1>", flip)
        draw()

    _ui_switch(tog_row, "描边", "border", ctx.read_ui_flag("border"))
    tk.Frame(tog_row, bg=p["card"], width=12).pack(side="left")
    _ui_switch(tog_row, "显示时间", "clock", ctx.read_ui_flag("clock"))
    tk.Frame(tog_row, bg=p["card"], width=12).pack(side="left")
    _ui_switch(tog_row, "动画", "anim", ctx.read_ui_flag("anim"))
    tk.Frame(tog_row, bg=p["card"], width=12).pack(side="left")
    _ui_switch(tog_row, "毛玻璃", "blur", ctx.read_ui_flag("blur", False))
    tk.Frame(tog_row, bg=p["card"], width=12).pack(side="left")
    _ui_switch(tog_row, "边缘吸附", "snap", ctx.read_ui_flag("snap"))
    tk.Frame(tog_row, bg=p["card"], width=12).pack(side="left")
    _ui_switch(tog_row, "悬停展开", "hover_expand", ctx.read_ui_flag("hover_expand", False))

    r = app._card_row(p, page, "⌨", "全局快捷键",
                      "看板贴在桌面层时，按这个组合唤出；录制后重启看板生效")
    app.hotkey_label = tk.Label(r, text=ctx.read_hotkey(), bg=p["card"], fg=p["text"],
                                font=("Consolas", 9))
    app.hotkey_label.pack(side="left", padx=(0, 10))
    tk.Button(r, text="录制", command=lambda: app._record_hotkey(
        "dash", "hotkey", app.hotkey_label, "ctrl+alt+d"), bg=p["btn"],
        fg=p["btn_text"], bd=0, padx=12, pady=4, cursor="hand2",
        font=("Microsoft YaHei UI", 8)).pack(side="left")

    r = app._card_row(p, page, "🗄", "聊天数据库", str(ctx.db))
    tk.Label(r, text="只读", bg=p["card"], fg=p["ok"],
             font=("Microsoft YaHei UI", 8, "bold")).pack(side="left")
    tk.Button(r, text="恢复", command=app.on_restore_db, bg=p["btn"],
              fg=p["btn_text"], bd=0, padx=12, pady=4, cursor="hand2",
              font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(10, 0))
    tk.Button(r, text="备份", command=app.on_backup_db, bg=p["btn"],
              fg=p["btn_text"], bd=0, padx=12, pady=4, cursor="hand2",
              font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(6, 0))

    unmarked = app._unmarked_count()
    r = app._card_row(p, page, "🧹", "未标记消息",
                      "txt 导入的老消息没有来源群；清理只删消息，已抽出的待办保留")
    tk.Label(r, text=f"{unmarked} 条", bg=p["card"],
             fg=p["muted"] if unmarked else p["ok"],
             font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=(0, 10))
    if unmarked:
        tk.Button(r, text="清理", command=app.on_purge_unmarked, bg=p["btn"],
                  fg=p["btn_text"], bd=0, padx=14, pady=4, cursor="hand2",
                  font=("Microsoft YaHei UI", 9)).pack(side="left")

    weekly = app._weekly_stats()
    r = app._card_row(p, page, "📊", "每周摘要",
                      "近 7 天的待办进出与最活跃群（群名来自同步时的群名映射）")
    if weekly:
        bits = [f"新增 {weekly['new']} 条", f"完成 {weekly['done']} 条"]
        if weekly.get("top_chat"):
            bits.append(f"最活跃：{weekly['top_chat'][0]}（{weekly['top_chat'][1]} 条消息）")
        tk.Label(r, text=" · ".join(bits), bg=p["card"], fg=p["text"],
                 font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
    else:
        tk.Label(r, text="还没有数据", bg=p["card"], fg=p["muted"],
                 font=("Microsoft YaHei UI", 9)).pack(side="left")

    cal = app._calibration_stats()
    r = app._card_row(p, page, "🎯", "校准反馈",
                      "标记待办为「非待办」或「完成」都自动写入，够 3 条样本后自动生效")
    st = cal.get("stats", {})
    total = st.get("total", 0)
    if total == 0:
        tk.Label(r, text="还没攒下校准。在看板或控制台把几条误抽的点掉就开始了",
                 bg=p["card"], fg=p["muted"],
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
    else:
        bits = [f"累计 {total} 条",
                f"确认 {st.get('confirmed', 0)}",
                f"拒绝 {st.get('rejected', 0)}"]
        bits.append(f"{st.get('unique_keys', 0)} 个来源")
        if cal.get("active_groups", 0) > 0:
            bits.append(f"其中 {cal['active_groups']} 组已够样本生效")
        tk.Label(r, text=" · ".join(bits), bg=p["card"], fg=p["text"],
                 font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        if cal.get("top_tags"):
            # 最大纠偏：列前 2 个 |adj| 最大的 reason_tag
            top = cal["top_tags"][:2]
            tag_str = " / ".join(
                f"{t.split('·')[-1] if '·' in t else t}({('+' if a > 0 else '')}{a})"
                for t, a in top)
            tk.Label(r, text=f"   最大纠偏: {tag_str}", bg=p["card"], fg=p["accent"],
                     font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(6, 0))
    tk.Button(r, text="导出", command=app.on_export_calibration,
              bg=p["btn"], fg=p["btn_text"], bd=0, padx=10, pady=3, cursor="hand2",
              font=("Microsoft YaHei UI", 8)).pack(side="right", padx=(6, 0))
    tk.Button(r, text="导入", command=app.on_import_calibration,
              bg=p["btn"], fg=p["btn_text"], bd=0, padx=10, pady=3, cursor="hand2",
              font=("Microsoft YaHei UI", 8)).pack(side="right")

    has_key = (ctx.root / "dbkey.dat").exists()
    r = app._card_row(
        p, page, "🔑", "解密口令",
        "已保存（DPAPI 加密，仅本机当前用户可解）；留空表示不修改"
        if has_key else "尚未保存，首次同步前必须填写")
    app.pw_entry = tk.Entry(r, width=24, show="*", bg=p["field"], fg=p["field_text"],
                            insertbackground=p["field_text"], bd=0, relief="flat",
                            highlightthickness=1, highlightbackground=p["border"],
                            highlightcolor=p["accent"], font=("Microsoft YaHei UI", 9))
    app.pw_entry.pack(side="left", ipady=4)

    chat_names = ctx.read_chat_names()
    app.ignore_state = app._chip_card(
        p, page, "🚫", "免打扰（不统计）",
        "这些群号或 QQ 号（如群机器人）的消息完全不参与抽取；重复添加会自动拦住",
        ctx.read_filter("ignore_groups"), "群号或 QQ 号", names=chat_names)

    app.star_state = app._chip_card(
        p, page, "★", "特别关心（标重点）",
        "群号或 QQ 号；这些来源产生的待办会标记为 ★重点，看板里有独立标签页",
        ctx.read_filter("star_groups"), "群号或 QQ 号", names=chat_names)

    if not chat_names:
        # 库里还没有群名映射：后台去 QQ 库抓一次，抓完把两张卡片上的号码换成群名
        threading.Thread(target=app._refresh_chat_names, daemon=True).start()

    r = app._card_row(p, page, "⏱", "定时同步", "由 Windows 计划任务每小时触发")
    tk.Button(r, text="打开计划任务", command=lambda: subprocess.Popen(
        ["control", "schedtasks"]), bg=p["btn"], fg=p["btn_text"], bd=0,
        padx=12, pady=4, cursor="hand2",
        font=("Microsoft YaHei UI", 8)).pack(side="left")
