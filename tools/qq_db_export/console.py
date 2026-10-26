"""Chat2Todo 控制台：一个窗口管所有事（tkinter，标准库实现）。

功能：
- 立即同步（内置引擎只读 QQ 库 → 入库 → AI 复核，不再需要 Docker），日志实时滚动
- 打开看板 / 导入 txt / 目录监视开关
- 设置：Ollama 地址与模型、口令（DPAPI 加密保存）

设计原则：所有重活都交给子进程，界面永远不阻塞；出错只提示，不崩溃。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"           # 仓库源码（src 布局，免 pip install 直接跑）
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
ROOT = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Chat2Todo"
CONFIG = ROOT / "c2t.toml"
DB = ROOT / "qq.db"
PY = sys.executable

# 设置窗口的 AI 服务商预设：均为 OpenAI 兼容端点（核心 ai.py 用 /chat/completions + Bearer）
AI_PRESETS = {
    "本地 Ollama（免费无需 Key）": ("http://127.0.0.1:11434/v1", "qwen2.5:14b"),
    "DeepSeek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "豆包（火山方舟）": ("https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-1-6-flash"),
    "通义千问（阿里云百炼）": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "OpenAI（GPT / Codex 系列）": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "Claude（经 OpenRouter 中转）": ("https://openrouter.ai/api/v1", "anthropic/claude-sonnet-4.5"),
    "自定义": ("", ""),
}

# 浅色 / 深色调色板：键名固定，界面各处只从这里取色
THEMES = {
    "light": {
        "bg": "#f4f6f9", "card": "#ffffff", "text": "#1f2937", "muted": "#8a94a3",
        "title": "#1f3a5f", "border": "#dfe5ec",
        "field": "#ffffff", "field_text": "#1f2937",
        "btn": "#eef2f7", "btn_text": "#1f3a5f",
        "accent": "#1f6feb", "accent_hover": "#1859c4", "accent_fg": "#ffffff",
        "ok": "#15803d", "err": "#b91c1c",
        "log_bg": "#111418", "log_fg": "#d7dce3",
        "list_bg": "#ffffff", "list_fg": "#1f2937",
    },
    "dark": {
        "bg": "#171b22", "card": "#1f242d", "text": "#e3e8f0", "muted": "#98a2b3",
        "title": "#8ab4f8", "border": "#333c48",
        "field": "#262d37", "field_text": "#e3e8f0",
        "btn": "#2b333d", "btn_text": "#cfd8e6",
        "accent": "#3b82f6", "accent_hover": "#2f6fd0", "accent_fg": "#ffffff",
        "ok": "#4ade80", "err": "#f87171",
        "log_bg": "#0d1117", "log_fg": "#d7dce3",
        "list_bg": "#262d37", "list_fg": "#e3e8f0",
    },
}


def _read_toml_key(section_name: str, key: str) -> str:
    """从 c2t.toml 读某段某键的字符串值；不存在返回空串。"""
    if not CONFIG.exists():
        return ""
    section = ""
    for line in CONFIG.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip()
            continue
        if section == section_name and "=" in s:
            k, v = s.split("=", 1)
            if k.strip() == key:
                return v.strip().strip("'\"")
    return ""


def _read_theme() -> str:
    """读 [ui] theme，默认 light。"""
    return "dark" if _read_toml_key("ui", "theme").lower() == "dark" else "light"


def _env() -> dict:
    """子进程环境：注入 src 路径，不依赖安装；强制 UTF-8 防止中文乱码。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def _stat() -> dict:
    """读库内统计；库不存在时返回空。"""
    import sqlite3

    if not DB.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
            return {
                "messages": q("SELECT COUNT(*) FROM messages"),
                "todos": q("SELECT COUNT(*) FROM todos"),
                "pending": q("SELECT COUNT(*) FROM todos WHERE status='pending'"),
                "unconfirmed": q("SELECT COUNT(*) FROM todos WHERE status='unconfirmed'"),
            }
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return {}


def _read_cfg() -> dict:
    """极简 TOML 读取：只要 [ai] 段的几个键。"""
    out = {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:14b",
           "api_key": "", "enabled": False}
    if not CONFIG.exists():
        return out
    section = ""
    for line in CONFIG.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip()
            continue
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip().strip("'\"")
        if section == "ai" and k in out:
            out[k] = v
    return out


def _read_snap_model() -> str:
    """读 [snap] vision_model（空=只用系统 OCR）。"""
    return _read_toml_key("snap", "vision_model")


def _apply_ui_style(style: ttk.Style, theme: str) -> None:
    """把主题写进 ttk 样式——设置面板完全由样式驱动，改这里等于整体换肤。"""
    p = THEMES.get(theme, THEMES["light"])
    try:
        style.theme_use("clam")            # clam 允许自定义背景色，Windows 原生主题不允许
    except tk.TclError:
        pass
    style.configure(".", background=p["bg"], foreground=p["text"],
                    font=("Microsoft YaHei UI", 9))
    style.configure("Page.TFrame", background=p["bg"])
    style.configure("Card.TLabelframe", background=p["card"], bordercolor=p["border"],
                    relief="solid", borderwidth=1)
    style.configure("Card.TLabelframe.Label", background=p["card"], foreground=p["title"],
                    font=("Microsoft YaHei UI", 9, "bold"))
    style.configure("Card.TFrame", background=p["card"])
    style.configure("Card.TLabel", background=p["card"], foreground=p["text"])
    style.configure("Hint.TLabel", background=p["card"], foreground=p["muted"],
                    font=("Microsoft YaHei UI", 8))
    style.configure("Status.TLabel", background=p["card"], foreground=p["muted"],
                    font=("Microsoft YaHei UI", 8))
    style.configure("Ok.TLabel", background=p["card"], foreground=p["ok"],
                    font=("Microsoft YaHei UI", 8))
    style.configure("Err.TLabel", background=p["card"], foreground=p["err"],
                    font=("Microsoft YaHei UI", 8))
    style.configure("Title.TLabel", background=p["bg"], foreground=p["title"],
                    font=("Microsoft YaHei UI", 14, "bold"))
    style.configure("Sub.TLabel", background=p["bg"], foreground=p["muted"],
                    font=("Microsoft YaHei UI", 8))
    style.configure("TEntry", fieldbackground=p["field"], foreground=p["field_text"],
                    insertcolor=p["field_text"], bordercolor=p["border"],
                    lightcolor=p["border"], darkcolor=p["border"])
    style.configure("TCombobox", fieldbackground=p["field"], foreground=p["field_text"],
                    background=p["card"], arrowcolor=p["text"], bordercolor=p["border"])
    style.map("TCombobox", fieldbackground=[("readonly", p["field"])],
              foreground=[("readonly", p["field_text"])])
    style.configure("TButton", padding=5, background=p["btn"], foreground=p["btn_text"],
                    bordercolor=p["border"])
    style.map("TButton", background=[("active", p["card"])])
    style.configure("Accent.TButton", font=("Microsoft YaHei UI", 9, "bold"), padding=6,
                    foreground=p["accent_fg"], background=p["accent"], borderwidth=0)
    style.map("Accent.TButton", background=[("active", p["accent_hover"])])


def _toml_val(v) -> str:
    """TOML 字面量：bool 写 true/false，其余写带引号字符串。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    return f"'{v}'"


def _write_section(section: str, cfg: dict) -> None:
    """只改指定段里的给定键，其余内容原样保留。"""
    ROOT.mkdir(parents=True, exist_ok=True)
    lines = CONFIG.read_text(encoding="utf-8-sig").splitlines() if CONFIG.exists() else []

    start = end = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if s[1:-1].strip() == section and start < 0:
                start = i
            elif start >= 0 and end < 0:
                end = i
                break
    if start >= 0:
        if end < 0:
            end = len(lines)
        block = lines[start + 1:end]
        seen = set()
        new_block = []
        for line in block:
            s = line.strip()
            if "=" in s and not s.startswith("#"):
                k = s.split("=", 1)[0].strip()
                if k in cfg:
                    new_block.append(f"{k} = {_toml_val(cfg[k])}")
                    seen.add(k)
                    continue
            new_block.append(line)
        for k in cfg:
            if k not in seen:
                new_block.append(f"{k} = {_toml_val(cfg[k])}")
        lines[start + 1:end] = new_block
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        for k, v in cfg.items():
            lines.append(f"{k} = {_toml_val(v)}")
    CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_cfg(cfg: dict) -> None:
    _write_section("ai", cfg)


def _read_hotkey(section: str = "dash", key: str = "hotkey",
                 default: str = "ctrl+alt+d") -> str:
    """读某个段里的快捷键配置（[dash] hotkey 或 [snap] hotkey）。"""
    try:
        from chat2todo.config import load

        return (load().get(section) or {}).get(key) or default
    except Exception:  # noqa: BLE001
        return default


def _probe_ai(base: str, key: str, model: str) -> tuple[bool, str, list[str]]:
    """探测 AI 端点：本地 Ollama 查 /api/tags，云端查 /models。

    返回 (成功, 提示文字, 可用模型列表)。只读 GET，不发任何聊天内容。
    """
    import json
    import urllib.request

    base = (base or "").rstrip("/")
    if not base:
        return False, "还没有填 API 地址", []
    local = base.startswith(("http://127.0.0.1", "http://localhost"))
    root = base[:-3] if base.endswith("/v1") else base
    url = f"{root}/api/tags" if local else f"{base}/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key or 'ollama'}"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"连接失败：{exc}", []
    if local:
        names = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    else:
        names = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
    if model and names and model not in names:
        return True, f"连接成功，但列表里没有「{model}」", names
    return True, f"连接成功 · 可用模型 {len(names)} 个", names


class Console(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Chat2Todo 控制台")
        self.geometry("820x580")
        self.minsize(720, 500)

        self.syncing = False
        self.watch_proc: subprocess.Popen | None = None
        self.snap_proc: subprocess.Popen | None = None
        self.theme = _read_theme()

        self._build_ui()
        self.refresh_stats()

    # ---------- 界面 ----------
    def _pal(self) -> dict:
        return THEMES.get(self.theme, THEMES["light"])

    def _card_row(self, p: dict, parent, icon: str, title: str, sub: str):
        """卡片行（与设置窗同款）：图标 · 标题/说明 · 右侧控件区。"""
        outer = tk.Frame(parent, bg=p["card"], highlightbackground=p["border"],
                         highlightthickness=1)
        outer.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(outer, bg=p["card"])
        inner.pack(fill="x", padx=14, pady=12)
        tk.Label(inner, text=icon, bg=p["card"], fg=p["title"],
                 font=("Segoe UI Emoji", 13)).pack(side="left", padx=(0, 12))
        texts = tk.Frame(inner, bg=p["card"])
        texts.pack(side="left", fill="x", expand=True)
        tk.Label(texts, text=title, bg=p["card"], fg=p["text"], anchor="w",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        if sub:
            tk.Label(texts, text=sub, bg=p["card"], fg=p["muted"], anchor="w",
                     justify="left", wraplength=420,
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(1, 0))
        right = tk.Frame(inner, bg=p["card"])
        right.pack(side="right")
        return right

    def _pill(self, p: dict, parent, text: str, cmd, primary: bool = False):
        b = tk.Button(parent, text=text, command=cmd, relief="flat", cursor="hand2",
                      bg=(p["accent"] if primary else p["btn"]),
                      fg=(p["accent_fg"] if primary else p["btn_text"]),
                      activebackground=(p["accent_hover"] if primary else p["border"]),
                      activeforeground=(p["accent_fg"] if primary else p["text"]),
                      font=("Microsoft YaHei UI", 9, "bold" if primary else "normal"),
                      padx=14, pady=5, borderwidth=0, highlightthickness=0)
        b.pack(side="left", padx=(6, 0))
        return b

    # ---------- 单窗口：侧栏 + 分页 ----------
    NAV = [
        ("console", "◈", "控制台"),
        ("general", "🏠", "常规"),
        ("ai", "✨", "AI 服务"),
        ("snap", "📷", "截图识别"),
        ("about", "ⓘ", "关于"),
    ]

    def _build_ui(self) -> None:
        p = self._pal()
        self.configure(bg=p["bg"])
        nav_bg = "#12151a" if self.theme == "dark" else "#e9edf3"
        self._nav_bg = nav_bg

        side = tk.Frame(self, bg=nav_bg, width=200)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="◈  Chat2Todo", bg=nav_bg, fg=p["title"], anchor="w",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(fill="x", padx=16, pady=(18, 14))

        self._nav = {}
        for key, icon, name in self.NAV:
            item = tk.Frame(side, bg=nav_bg, cursor="hand2")
            item.pack(fill="x", pady=2)
            bar = tk.Frame(item, bg=nav_bg, width=3)
            bar.pack(side="left", fill="y")
            inner = tk.Frame(item, bg=nav_bg)
            inner.pack(fill="x")
            ic = tk.Label(inner, text=icon, bg=nav_bg, fg=p["muted"],
                          font=("Segoe UI Emoji", 11), width=3)
            ic.pack(side="left", padx=(8, 0), pady=8)
            tx = tk.Label(inner, text=name, bg=nav_bg, fg=p["muted"], anchor="w",
                          font=("Microsoft YaHei UI", 10))
            tx.pack(side="left", fill="x", expand=True)
            for w in (item, inner, ic, tx):
                w.bind("<Button-1>", lambda _e, k=key: self._show_page(k))
            self._nav[key] = (item, bar, ic, tx)

        right = tk.Frame(self, bg=p["bg"])
        right.pack(side="left", fill="both", expand=True)

        head = tk.Frame(right, bg=p["bg"])
        head.pack(fill="x", padx=26, pady=(22, 8))
        self.page_title = tk.Label(head, text="控制台", bg=p["bg"], fg=p["text"], anchor="w",
                                   font=("Microsoft YaHei UI", 17, "bold"))
        self.page_title.pack(anchor="w")
        self.page_sub = tk.Label(head, text="", bg=p["bg"], fg=p["muted"], anchor="w",
                                 font=("Microsoft YaHei UI", 9))
        self.page_sub.pack(anchor="w", pady=(2, 0))
        self.stats = tk.Label(head, text="", bg=p["bg"], fg=p["muted"], anchor="w",
                              font=("Microsoft YaHei UI", 9))
        self.stats.pack(anchor="w", pady=(2, 0))

        self.body = tk.Frame(right, bg=p["bg"])
        self.body.pack(fill="both", expand=True, padx=26)

        foot = tk.Frame(right, bg=p["bg"])
        foot.pack(fill="x", padx=26, pady=(4, 16))
        self.save_btn = tk.Button(foot, text="保存设置", bg=p["accent"], fg=p["accent_fg"],
                                  bd=0, padx=22, pady=6, cursor="hand2",
                                  font=("Microsoft YaHei UI", 9, "bold"),
                                  command=self._save_settings)
        self.save_btn.pack(side="right")

        self._pages = {}
        self._build_page_console()
        self._build_page_general()
        self._build_page_ai()
        self._build_page_snap()
        self._build_page_about()

        self._show_page(getattr(self, "_current_page", "console"))

    def _new_page(self) -> tk.Frame:
        f = tk.Frame(self.body, bg=self._pal()["bg"])
        return f

    def _show_page(self, key: str) -> None:
        p = self._pal()
        self._current_page = key
        for frame in self._pages.values():
            frame.pack_forget()
        self._pages[key].pack(fill="both", expand=True)
        label = next(n for n in self.NAV if n[0] == key)
        self.page_title.config(text=label[2])
        sub = {
            "console": "同步、看板与运行日志",
            "general": "外观、数据库口令与定时同步",
            "ai": "服务商、密钥与连接测试",
            "snap": "截图待办使用的识别引擎",
            "about": "版本与数据位置",
        }.get(key, "")
        self.page_sub.config(text=sub)
        self.stats.pack_forget()
        if key == "console":
            self.stats.pack(anchor="w", pady=(2, 0))
        for k, (item, bar, ic, tx) in self._nav.items():
            active = k == key
            bg = p["card"] if active else self._nav_bg
            fg = p["text"] if active else p["muted"]
            item.configure(bg=bg)
            item.master  # noqa: B018 - 保持引用
            bar.configure(bg=(p["accent"] if active else bg))
            ic.configure(bg=bg, fg=fg)
            tx.configure(bg=bg, fg=fg)
            item.winfo_children()[1].configure(bg=bg)

    def set_theme(self, theme: str) -> None:
        """切换主题：整窗重建（保留日志与当前页）。"""
        if theme == self.theme:
            return
        self.theme = theme if theme in THEMES else "light"
        keep = self.log.get("1.0", "end-1c") if hasattr(self, "log") else ""
        for child in self.winfo_children():
            if isinstance(child, tk.Toplevel):
                continue
            child.destroy()
        self._build_ui()
        if keep:
            self.write(keep)
        self.refresh_stats()
        self.write(f"\n▸ 已切换到{'深色' if self.theme == 'dark' else '浅色'}模式\n")

    # ---------- 页：控制台 ----------
    def _build_page_console(self) -> None:
        p = self._pal()
        page = self._new_page()
        self._pages["console"] = page

        r = self._card_row(p, page, "⟳", "立即同步",
                           "从 QQ 数据库只读抓取 → 规则抽取 → 本地 AI 复核")
        self.sync_btn = self._pill(p, r, "立即同步", self.on_sync, primary=True)

        r = self._card_row(p, page, "▦", "快捷操作", "看板、导入与截图待办")
        self._pill(p, r, "打开看板", self.on_dashboard)
        self._pill(p, r, "导入文件", self.on_import)
        self._pill(p, r, "截图待办", self.on_snap)

        r = self._card_row(p, page, "⚙", "更多", "截图热键、目录监视与定时同步")
        self.snap_btn = self._pill(p, r, "截图热键", self.on_snap_hotkey)
        self.watch_btn = self._pill(p, r, "开始监视", self.on_watch)
        self._pill(p, r, "定时同步", self.on_schedule)

        outer = tk.Frame(page, bg=p["card"], highlightbackground=p["border"],
                         highlightthickness=1)
        outer.pack(fill="both", expand=True, pady=(0, 8))
        inner = tk.Frame(outer, bg=p["card"])
        inner.pack(fill="both", expand=True, padx=14, pady=12)
        tk.Label(inner, text="运行日志", bg=p["card"], fg=p["text"], anchor="w",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(0, 6))
        self.log = tk.Text(inner, bg=p["log_bg"], fg=p["log_fg"],
                           insertbackground=p["log_fg"], relief="flat",
                           font=("Consolas", 9), wrap="word", borderwidth=0,
                           padx=12, pady=10, height=8)
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

    # ---------- 页：常规 ----------
    def _build_page_general(self) -> None:
        p = self._pal()
        page = self._new_page()
        self._pages["general"] = page
        cfg = _read_cfg()

        r = self._card_row(p, page, "🎨", "深色模式", "开启后整个控制台与看板切换为深色")
        mk = tk.Canvas(r, width=46, height=24, bg=p["card"], highlightthickness=0,
                       cursor="hand2")
        mk.pack(side="left")
        state = {"on": self.theme == "dark"}

        def draw_switch() -> None:
            mk.delete("all")
            on = state["on"]
            track = p["accent"] if on else ("#c7ccd6" if self.theme == "light" else "#3a4553")
            mk.create_oval(2, 4, 22, 24, fill=track, outline="")
            mk.create_oval(24, 4, 44, 24, fill=track, outline="")
            mk.create_rectangle(12, 4, 34, 24, fill=track, outline="")
            x = 32 if on else 12
            mk.create_oval(x - 9, 3, x + 9, 21, fill="#ffffff", outline="")

        def flip(_e=None) -> None:
            state["on"] = not state["on"]
            draw_switch()
            t = "dark" if state["on"] else "light"
            _write_section("ui", {"theme": t})
            self._show_page("general")
            self.set_theme(t)

        mk.bind("<Button-1>", flip)
        draw_switch()

        r = self._card_row(p, page, "⌨", "全局快捷键",
                           "看板贴在桌面层时，按这个组合唤出；录制后重启看板生效")
        self.hotkey_label = tk.Label(r, text=_read_hotkey(), bg=p["card"], fg=p["text"],
                                     font=("Consolas", 9))
        self.hotkey_label.pack(side="left", padx=(0, 10))
        tk.Button(r, text="录制", command=lambda: self._record_hotkey("dash", "hotkey", self.hotkey_label, "ctrl+alt+d"), bg=p["btn"],
                  fg=p["btn_text"], bd=0, padx=12, pady=4, cursor="hand2",
                  font=("Microsoft YaHei UI", 8)).pack(side="left")

        r = self._card_row(p, page, "🗄", "聊天数据库", str(DB))
        tk.Label(r, text="只读", bg=p["card"], fg=p["ok"],
                 font=("Microsoft YaHei UI", 8, "bold")).pack(side="left")

        has_key = (ROOT / "dbkey.dat").exists()
        r = self._card_row(
            p, page, "🔑", "解密口令",
            "已保存（DPAPI 加密，仅本机当前用户可解）；留空表示不修改"
            if has_key else "尚未保存，首次同步前必须填写")
        self.pw_entry = tk.Entry(r, width=24, show="*", bg=p["field"], fg=p["field_text"],
                                 insertbackground=p["field_text"], bd=0, relief="flat",
                                 highlightthickness=1, highlightbackground=p["border"],
                                 highlightcolor=p["accent"], font=("Microsoft YaHei UI", 9))
        self.pw_entry.pack(side="left", ipady=4)

        r = self._card_row(p, page, "⏱", "定时同步", "由 Windows 计划任务每小时触发")
        tk.Button(r, text="打开计划任务", command=lambda: subprocess.Popen(
            ["control", "schedtasks"]), bg=p["btn"], fg=p["btn_text"], bd=0,
            padx=12, pady=4, cursor="hand2",
            font=("Microsoft YaHei UI", 8)).pack(side="left")

    # ---------- 页：AI ----------
    def _build_page_ai(self) -> None:
        p = self._pal()
        page = self._new_page()
        self._pages["ai"] = page
        cfg = _read_cfg()
        ai_ok = str(cfg.get("enabled")).lower() == "true"
        local_now = str(cfg.get("base_url", "")).startswith(("http://127.0.0.1", "http://localhost"))

        r = self._card_row(
            p, page, "✨", "服务状态",
            f"已启用 · {'本地 Ollama' if local_now else '云端'} · {cfg.get('model', '')}"
            if ai_ok else "未启用（仅用规则抽取，不联网）")
        tk.Label(r, text="● 运行中" if ai_ok else "○ 未启用", bg=p["card"],
                 fg=p["ok"] if ai_ok else p["muted"],
                 font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")

        r = self._card_row(p, page, "🏷", "服务商", "选择后自动填入地址与模型")
        preset = ttk.Combobox(r, values=list(AI_PRESETS), state="readonly", width=22,
                              font=("Microsoft YaHei UI", 9))
        preset.pack(side="left")
        preset.set("本地 Ollama（免费无需 Key）")
        self._preset = preset

        r = self._card_row(p, page, "🌐", "API 地址", "OpenAI 兼容端点")
        self.base_entry = tk.Entry(r, width=30, bg=p["field"], fg=p["field_text"],
                                   insertbackground=p["field_text"], bd=0, relief="flat",
                                   highlightthickness=1, highlightbackground=p["border"],
                                   font=("Microsoft YaHei UI", 9))
        self.base_entry.pack(side="left", ipady=4)
        self.base_entry.insert(0, cfg["base_url"])

        r = self._card_row(p, page, "🧠", "模型", "本地建议 qwen2.5:14b")
        self.model_entry = tk.Entry(r, width=26, bg=p["field"], fg=p["field_text"],
                                    insertbackground=p["field_text"], bd=0, relief="flat",
                                    highlightthickness=1, highlightbackground=p["border"],
                                    font=("Microsoft YaHei UI", 9))
        self.model_entry.pack(side="left", ipady=4)
        self.model_entry.insert(0, cfg["model"])

        r = self._card_row(p, page, "🔐", "API Key", "本地 Ollama 可留空（自动用占位值）")
        self.key_entry = tk.Entry(r, width=30, show="*", bg=p["field"], fg=p["field_text"],
                                  insertbackground=p["field_text"], bd=0, relief="flat",
                                  highlightthickness=1, highlightbackground=p["border"],
                                  font=("Microsoft YaHei UI", 9))
        self.key_entry.pack(side="left", ipady=4)
        if cfg["api_key"] and cfg["api_key"] != "ollama":
            self.key_entry.insert(0, cfg["api_key"])

        r = self._card_row(p, page, "🔌", "连接测试", "会向服务端发一次最小请求")
        self.probe_btn = tk.Button(r, text="测试连接", bg=p["accent"], fg=p["accent_fg"],
                                   bd=0, padx=14, pady=4, cursor="hand2",
                                   font=("Microsoft YaHei UI", 9), command=self._do_probe)
        self.probe_btn.pack(side="left")
        self.hint = tk.Label(r, text="", bg=p["card"], fg=p["muted"],
                             font=("Microsoft YaHei UI", 8))
        self.hint.pack(side="left", padx=(10, 0))

        def on_preset(_evt=None) -> None:
            base, model = AI_PRESETS.get(self._preset.get(), ("", ""))
            if not base:
                return
            self.base_entry.delete(0, "end"); self.base_entry.insert(0, base)
            self.model_entry.delete(0, "end"); self.model_entry.insert(0, model)

        preset.bind("<<ComboboxSelected>>", on_preset)

    def _record_hotkey(self, section: str = "dash", cfg_key: str = "hotkey",
                       label=None, default: str = "ctrl+alt+d") -> None:
        """弹出录制窗：按下组合键即写入 [section] cfg_key。"""
        p = self._pal()
        win = tk.Toplevel(self)
        win.title("录制快捷键")
        win.geometry("360x150")
        win.transient(self)
        win.configure(bg=p["bg"])
        win.grab_set()
        tk.Label(win, text="请按下组合键", bg=p["bg"], fg=p["text"],
                 font=("Microsoft YaHei UI", 12, "bold")).pack(pady=(18, 4))
        hint = tk.Label(win, text="至少包含 Ctrl / Alt / Shift 之一，例：Ctrl+Alt+D",
                        bg=p["bg"], fg=p["muted"], font=("Microsoft YaHei UI", 8))
        hint.pack()

        def on_key(e) -> None:
            import ctypes

            if e.keysym in ("Control_L", "Control_R", "Alt_L", "Alt_R",
                            "Shift_L", "Shift_R", "Win_L", "Win_R"):
                return
            u = ctypes.windll.user32

            def down(vk: int) -> bool:
                return bool(u.GetAsyncKeyState(vk) & 0x8000)

            parts = []
            if down(0x11):
                parts.append("ctrl")
            if down(0x12):
                parts.append("alt")
            if down(0x10):
                parts.append("shift")
            if down(0x5B) or down(0x5C):
                parts.append("win")
            pressed = e.keysym.lower()
            if not (len(pressed) == 1 and pressed.isalnum()) and not (
                    pressed.startswith("f") and pressed[1:].isdigit()):
                hint.config(text="只支持字母 / 数字 / F1-F12", fg=p["err"])
                return
            if not parts:
                hint.config(text="必须带 Ctrl / Alt / Shift / Win 至少一个", fg=p["err"])
                return
            spec = "+".join(parts + [pressed])
            _write_section(section, {cfg_key: spec})
            if label is not None:
                label.config(text=spec)
            self.write(f"快捷键已改为 {spec}（重启对应功能生效）\n")
            win.destroy()

        win.bind("<KeyPress>", on_key)
        win.focus_force()

    def _do_probe(self) -> None:
        p = self._pal()
        base = self.base_entry.get().strip()
        model = self.model_entry.get().strip()
        key = self.key_entry.get().strip()
        self.hint.config(text="测试中…", fg=p["muted"])
        self.probe_btn.config(state="disabled")

        def run() -> None:
            ok, msg, models = _probe_ai(base, key, model)

            def show() -> None:
                self.hint.config(text=msg, fg=p["ok"] if ok else p["err"])
                self.probe_btn.config(state="normal")

            self.after(0, show)

        threading.Thread(target=run, daemon=True).start()

    # ---------- 页：截图 ----------
    def _build_page_snap(self) -> None:
        p = self._pal()
        page = self._new_page()
        self._pages["snap"] = page

        r = self._card_row(
            p, page, "📷", "视觉模型",
            "留空=只用系统 OCR（离线零安装）；填视觉模型=看图直提，如 qwen2.5vl:7b")
        self.vision = ttk.Combobox(r, values=["（不使用）", _read_snap_model() or ""],
                                   width=24, font=("Microsoft YaHei UI", 9))
        self.vision.pack(side="left")
        self.vision.set(_read_snap_model() or "（不使用）")

        r = self._card_row(p, page, "⌨", "截图热键",
                           "按热键后拖动框选屏幕区域；点「录制」可改键")
        self.snap_hotkey_label = tk.Label(r, text=_read_hotkey("snap", "hotkey", "ctrl+alt+s"),
                                         bg=p["card"], fg=p["text"], font=("Consolas", 9))
        self.snap_hotkey_label.pack(side="left", padx=(0, 10))
        tk.Button(r, text="录制",
                  command=lambda: self._record_hotkey("snap", "hotkey",
                                                      self.snap_hotkey_label, "ctrl+alt+s"),
                  bg=p["btn"], fg=p["btn_text"], bd=0, padx=12, pady=4,
                  cursor="hand2", font=("Microsoft YaHei UI", 8)).pack(side="left")

    # ---------- 页：关于 ----------
    def _build_page_about(self) -> None:
        p = self._pal()
        page = self._new_page()
        self._pages["about"] = page
        try:
            from chat2todo import __version__ as ver
        except Exception:  # noqa: BLE001
            ver = "—"
        for icon, title, sub in (
            ("◈", "Chat2Todo", f"版本 {ver} · 本地优先的聊天记录待办提取器"),
            ("💾", "数据目录", str(ROOT)),
            ("📄", "许可", "MIT License"),
        ):
            self._card_row(p, page, icon, title, sub)

    def _save_settings(self) -> None:
        cfg = _read_cfg()
        base = self.base_entry.get().strip() or cfg["base_url"]
        model = self.model_entry.get().strip() or cfg["model"]
        key = self.key_entry.get().strip()
        local = base.startswith(("http://127.0.0.1", "http://localhost"))
        if local and not key:
            key = "ollama"
        enabled = bool(base and key)
        _write_section("ai", {"base_url": base, "model": model, "api_key": key,
                              "enabled": enabled, "review": enabled})
        vm = self.vision.get().strip()
        _write_section("snap", {"vision_model": "" if vm == "（不使用）" else vm})
        pw = self.pw_entry.get().strip()
        if pw:
            try:
                from chat2todo.core import keybox

                keybox.save_key(pw)
                self.write("口令已加密保存（DPAPI，仅本机当前用户可解）\n")
            except Exception as exc:  # noqa: BLE001
                self.write(f"口令保存失败：{exc}\n")
        self.write("设置已保存（重启同步生效）\n")
        if enabled and not local:
            self.write("提示：已启用云端 AI 复核，待办文字会发送到云端 API（按量计费）\n")

    def write(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh_stats(self) -> None:
        s = _stat()
        if not s:
            self.stats.config(text="还没有数据，点「立即同步」")
        else:
            self.stats.config(
                text=f"消息 {s['messages']} · 待办 {s['todos']} · 待处理 {s['pending']} · 待定 {s['unconfirmed']}"
            )

    # ---------- 动作 ----------
    def _stream(self, title: str, cmd: list[str]) -> None:
        self.write(f"\n▸ {title}\n$ {' '.join(cmd)}\n")

        def run() -> None:
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace", bufsize=1,
                                        env=_env())
                assert proc.stdout is not None
                for line in proc.stdout:
                    self.write(line)
                proc.wait()
                self.write(f"  ← 退出码 {proc.returncode}\n")
            except Exception as exc:  # noqa: BLE001
                self.write(f"  ✗ {exc}\n")
            finally:
                self.after(0, self.refresh_stats)

        threading.Thread(target=run, daemon=True).start()

    def on_schedule(self) -> None:
        """用 Windows 计划任务实现定时同步（关掉控制台也照跑）。"""
        task = "Chat2TodoSync"
        exists = subprocess.run(["schtasks", "/Query", "/TN", task],
                                capture_output=True, text=True).returncode == 0
        if exists:
            if not messagebox.askyesno("定时同步", f"已存在计划任务「{task}」（每 2 小时一次）。\n要关闭它吗？"):
                return
            r = subprocess.run(["schtasks", "/Delete", "/TN", task, "/F"],
                               capture_output=True, text=True)
            self.write(f"\n▸ 已请求删除计划任务：{'成功' if r.returncode == 0 else r.stderr.strip()}\n")
            messagebox.showinfo("定时同步", "已关闭定时同步")
            return

        bat = Path(__file__).with_name("后台同步.bat")
        if not bat.exists():
            messagebox.showerror("定时同步", f"找不到 {bat.name}，请检查安装是否完整")
            return
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", task, "/SC", "HOURLY", "/MO", "2",
             "/TR", f'cmd /c "{bat}"', "/F"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            self.write(f"\n▸ 已开启定时同步：每 2 小时后台跑一次（任务名 {task}）\n")
            messagebox.showinfo("定时同步", "已开启：每 2 小时自动同步一次\n（后台运行，不弹看板）")
        else:
            self.write(f"\n▸ 创建计划任务失败：{r.stderr.strip() or r.stdout.strip()}\n")
            messagebox.showerror("定时同步", "创建失败，可能需要在「以管理员身份运行」下再试")

    def on_sync(self) -> None:
        if self.syncing:
            messagebox.showinfo("正在同步", "同步还没结束，稍等一下")
            return
        self.syncing = True
        self.sync_btn.config(state="disabled", text="同步中…")

        def worker() -> None:
            cmd = [PY, "-u", "-m", "chat2todo", "db-import", "--days", "7", "--db", str(DB)]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace", bufsize=1,
                                        env=_env())
                assert proc.stdout is not None
                for line in proc.stdout:
                    self.write(line)
                proc.wait()
            except Exception as exc:  # noqa: BLE001
                self.write(f"✗ {exc}\n")
            finally:
                self.syncing = False
                self.sync_btn.config(state="normal", text="立即同步")
                self.after(0, self.refresh_stats)

        self.write("\n========== 开始同步 ==========\n")
        threading.Thread(target=worker, daemon=True).start()

    def on_dashboard(self) -> None:
        try:
            subprocess.Popen([PY, "-m", "chat2todo", "dash", "--db", str(DB), "--interval", "30"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=_env(),
                             creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            self.write("已打开看板\n")
        except Exception as exc:  # noqa: BLE001
            self.write(f"看板打开失败：{exc}\n")

    def on_import(self) -> None:
        path = filedialog.askopenfilename(title="选择聊天记录导出文件",
                                          filetypes=[("文本/导出文件", "*.txt *.mht *.html *.json"), ("全部", "*.*")])
        if not path:
            return
        self._stream("导入文件", [PY, "-m", "chat2todo", "import", path, "--db", str(DB)])

    def on_snap(self) -> None:
        """截图待办：独立小窗进程，不阻塞控制台（自带确认弹窗）。"""
        snap = HERE.parent / "snap_todo" / "snap.py"
        if not snap.exists():
            messagebox.showerror("截图待办", f"找不到 {snap}\n请检查 tools/snap_todo 是否完整")
            return
        try:
            subprocess.Popen([PY, str(snap), "--capture"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_env())
            self.write("已启动截图待办：拖动框选屏幕区域即可识别\n")
        except Exception as exc:  # noqa: BLE001
            self.write(f"截图待办启动失败：{exc}\n")

    def on_snap_hotkey(self) -> None:
        """截图热键开关：常驻注册 Ctrl+Alt+S；再点一次停止。"""
        if self.snap_proc and self.snap_proc.poll() is None:
            self.snap_proc.terminate()
            self.snap_proc = None
            self.snap_btn.config(text="截图热键")
            self.write("已停止截图热键\n")
            return
        snap = HERE.parent / "snap_todo" / "snap.py"
        if not snap.exists():
            messagebox.showerror("截图热键", f"找不到 {snap}\n请检查 tools/snap_todo 是否完整")
            return
        try:
            self.snap_proc = subprocess.Popen(
                [PY, str(snap), "--hotkey"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_env(),
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            self.snap_btn.config(text="停止热键")
            self.write("截图热键已常驻：按热键后直接拖动框选屏幕区域\n"
                       "（控制台关闭后热键仍有效；回控制台再点本按钮即可停止）\n")
        except Exception as exc:  # noqa: BLE001
            self.write(f"截图热键启动失败：{exc}\n")

    def on_watch(self) -> None:
        if self.watch_proc and self.watch_proc.poll() is None:
            self.watch_proc.terminate()
            self.watch_proc = None
            self.watch_btn.config(text="开始监视")
            self.write("已停止监视\n")
            return
        desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
        try:
            self.watch_proc = subprocess.Popen(
                [PY, "-m", "chat2todo", "watch", "--dir", str(desktop), "--interval", "5",
                 "--db", str(DB)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=_env(),
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            self.watch_btn.config(text="停止监视")
            self.write(f"开始监视桌面：{desktop}\n")
        except Exception as exc:  # noqa: BLE001
            self.write(f"监视启动失败：{exc}\n")

    def on_settings(self) -> None:
        """兼容旧入口：直接切到「常规」页。"""
        self._show_page("general")


def main() -> int:
    try:
        app = Console()
    except tk.TclError as exc:
        print(f"无法创建窗口：{exc}", file=sys.stderr)
        return 1
    app.write("欢迎使用 Chat2Todo 控制台。\n点「立即同步」从 QQ 数据库抓取并清洗待办。\n")
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
