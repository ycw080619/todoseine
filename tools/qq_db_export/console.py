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
        self.geometry("640x520")
        self.minsize(560, 440)

        self.syncing = False
        self.watch_proc: subprocess.Popen | None = None
        self.snap_proc: subprocess.Popen | None = None
        self.theme = _read_theme()

        self._build_ui()
        self.refresh_stats()

    # ---------- 界面 ----------
    def _pal(self) -> dict:
        return THEMES.get(self.theme, THEMES["light"])

    def _build_ui(self) -> None:
        p = self._pal()
        self.configure(bg=p["bg"])
        self._build_header(p)
        self._build_buttons(p)
        self._build_log(p)

    def set_theme(self, theme: str) -> None:
        """切换主题：整窗重建（保留日志内容与各按钮运行状态）。"""
        if theme == self.theme:
            return
        self.theme = theme if theme in THEMES else "light"
        keep = self.log.get("1.0", "end-1c") if hasattr(self, "log") else ""
        syncing, watching = self.syncing, bool(self.watch_proc and self.watch_proc.poll() is None)
        hotkey = bool(self.snap_proc and self.snap_proc.poll() is None)
        for child in self.winfo_children():
            if isinstance(child, tk.Toplevel):   # 设置窗等独立窗口不受换肤影响
                continue
            child.destroy()
        self._build_ui()
        if keep:
            self.write(keep)
        self.refresh_stats()
        if syncing:
            self.sync_btn.config(state="disabled", text="同步中…")
        if watching:
            self.watch_btn.config(text="停止监视")
        if hotkey:
            self.snap_btn.config(text="停止热键")
        self.write(f"\n▸ 已切换到{'深色' if self.theme == 'dark' else '浅色'}模式\n")

    def _build_header(self, p: dict) -> None:
        box = tk.Frame(self, bg=p["bg"])
        box.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(box, text="Chat2Todo", bg=p["bg"], fg=p["title"],
                 font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        self.stats = tk.Label(box, text="", bg=p["bg"], fg=p["muted"],
                              font=("Microsoft YaHei UI", 9))
        self.stats.pack(side="right")

    def _build_buttons(self, p: dict) -> None:
        box = tk.Frame(self, bg=p["bg"])
        box.pack(fill="x", padx=10, pady=4)

        def btn(text, cmd, primary=False):
            b = tk.Button(box, text=text, command=cmd, relief="flat", cursor="hand2",
                          bg=p["accent"] if primary else p["btn"],
                          fg=p["accent_fg"] if primary else p["btn_text"],
                          activebackground=p["accent_hover"] if primary else p["border"],
                          activeforeground=p["accent_fg"] if primary else p["btn_text"],
                          font=("Microsoft YaHei UI", 9), padx=12, pady=6,
                          borderwidth=0, highlightthickness=0)
            b.pack(side="left", padx=4, pady=2)
            return b

        self.sync_btn = btn("立即同步", self.on_sync, primary=True)
        btn("打开看板", self.on_dashboard)
        btn("导入文件", self.on_import)
        btn("截图待办", self.on_snap)
        self.snap_btn = btn("截图热键", self.on_snap_hotkey)
        self.watch_btn = btn("开始监视", self.on_watch)
        btn("定时同步", self.on_schedule)
        btn("设置", self.on_settings)

    def _build_log(self, p: dict) -> None:
        wrap = tk.Frame(self, bg=p["bg"])
        wrap.pack(fill="both", expand=True, padx=12, pady=(6, 12))
        self.log = tk.Text(wrap, bg=p["log_bg"], fg=p["log_fg"],
                           insertbackground=p["log_fg"], relief="flat",
                           font=("Consolas", 9), wrap="word", borderwidth=0)
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

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
            subprocess.Popen([PY, str(snap)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_env())
            self.write("已启动截图待办：先 Win+Shift+S 截图，再点按钮\n")
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
            self.write("截图热键已常驻：先 Win+Shift+S 截图，再按 Ctrl+Alt+S 唤出\n"
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
        """设置面板：卡片式分组（AI 服务商 / 截图识别 / 数据库），支持深色模式。"""
        cfg = _read_cfg()
        win = tk.Toplevel(self)
        win.title("设置 · Chat2Todo")
        win.geometry("660x700")
        win.minsize(620, 640)
        win.transient(self)

        style = ttk.Style(win)
        _apply_ui_style(style, self.theme)
        win.configure(bg=THEMES[self.theme]["bg"])

        def sync_listbox_colors(theme: str) -> None:
            p = THEMES[theme]
            win.option_add("*TCombobox*Listbox.background", p["list_bg"])
            win.option_add("*TCombobox*Listbox.foreground", p["list_fg"])
            win.option_add("*TCombobox*Listbox.selectBackground", p["accent"])
            win.option_add("*TCombobox*Listbox.selectForeground", p["accent_fg"])

        sync_listbox_colors(self.theme)

        shell = ttk.Frame(win, style="Page.TFrame")
        shell.pack(fill="both", expand=True, padx=16, pady=12)

        head = ttk.Frame(shell, style="Page.TFrame")
        head.pack(fill="x", pady=(0, 10))
        ttk.Label(head, text="设置", style="Title.TLabel").pack(anchor="w")
        ttk.Label(head, text="配置 AI 服务商、截图识别与数据库口令 · 保存后重启同步生效",
                  style="Sub.TLabel").pack(anchor="w")

        def card(title: str) -> ttk.Frame:
            box = ttk.LabelFrame(shell, text=f"  {title}  ", style="Card.TLabelframe")
            box.pack(fill="x", pady=(0, 10))
            body = ttk.Frame(box, style="Card.TFrame")
            body.pack(fill="x", padx=12, pady=(8, 10))
            body.columnconfigure(1, weight=1)
            return body

        def field(parent: ttk.Frame, r: int, label: str, value: str = "") -> ttk.Entry:
            ttk.Label(parent, text=label, style="Card.TLabel").grid(
                row=r, column=0, sticky="w", pady=5)
            e = ttk.Entry(parent)
            if value:
                e.insert(0, value)
            e.grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=5)
            return e

        # ---------- 卡片 1：AI 服务商 ----------
        ai = card("AI 服务商")
        ai_ok = str(cfg.get("enabled")).lower() == "true"
        local_now = str(cfg.get("base_url", "")).startswith(("http://127.0.0.1", "http://localhost"))
        status = ttk.Label(
            ai, style="Ok.TLabel" if ai_ok else "Status.TLabel",
            text=(f"● 已启用 · {'本地 Ollama' if local_now else '云端'} · {cfg.get('model', '')}"
                  if ai_ok else "○ 未启用（仅用规则抽取）"))
        status.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))

        ttk.Label(ai, text="服务商", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=5)
        preset = ttk.Combobox(ai, values=list(AI_PRESETS), state="readonly",
                              font=("Microsoft YaHei UI", 9))
        preset.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=5)
        preset.set("本地 Ollama（免费无需 Key）")

        base_entry = field(ai, 2, "API 地址", cfg["base_url"])
        model_entry = field(ai, 3, "模型", cfg["model"])
        key_entry = field(ai, 4, "API Key", cfg["api_key"])
        key_entry.config(show="*")

        def on_preset(_evt=None) -> None:
            base, model = AI_PRESETS.get(preset.get(), ("", ""))
            if not base:
                return
            base_entry.delete(0, "end"); base_entry.insert(0, base)
            model_entry.delete(0, "end"); model_entry.insert(0, model)
            hint.config(text="", style="Status.TLabel")

        preset.bind("<<ComboboxSelected>>", on_preset)
        ttk.Label(ai, text="国内：DeepSeek · 豆包 · 通义千问　　海外：OpenAI GPT/Codex · Claude（OpenRouter）",
                  style="Hint.TLabel").grid(row=5, column=1, sticky="w", padx=(12, 0))

        probe_bar = ttk.Frame(ai, style="Card.TFrame")
        probe_bar.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        probe_btn = ttk.Button(probe_bar, text="测试连接", style="Accent.TButton")
        probe_btn.pack(side="left")
        hint = ttk.Label(probe_bar, text="", style="Status.TLabel")
        hint.pack(side="left", padx=10)
        ttk.Label(ai, text="云端服务需到对应平台申请 Key（保存后明文存于本机配置文件）",
                  style="Hint.TLabel").grid(row=7, column=1, sticky="w", padx=(12, 0), pady=(4, 0))

        def do_probe() -> None:
            base = base_entry.get().strip()
            model = model_entry.get().strip()
            key = key_entry.get().strip()
            hint.config(text="测试中…", style="Status.TLabel")
            probe_btn.config(state="disabled")

            def run() -> None:
                ok, msg, models = _probe_ai(base, key, model)

                def show() -> None:
                    hint.config(text=msg, style="Ok.TLabel" if ok else "Err.TLabel")
                    probe_btn.config(state="normal")
                    if models:
                        vision.config(values=["（不使用）"] + models)

                win.after(0, show)

            threading.Thread(target=run, daemon=True).start()

        probe_btn.config(command=do_probe)

        # ---------- 卡片 2：截图识别 ----------
        snap_body = card("截图识别")
        ttk.Label(snap_body, text="视觉模型", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=5)
        vision = ttk.Combobox(snap_body, values=["（不使用）"], font=("Consolas", 9))
        vision.grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=5)
        vision.set(_read_snap_model() or "（不使用）")
        ttk.Label(snap_body,
                  text="留空=只用系统 OCR（离线零安装）；填视觉模型=看图直提，如 qwen2.5vl:7b",
                  style="Hint.TLabel", justify="left", wraplength=520).grid(
            row=1, column=1, sticky="w", padx=(12, 0), pady=(2, 0))

        # ---------- 卡片 3：数据库 ----------
        db_body = card("数据库")
        has_key = (ROOT / "dbkey.dat").exists()
        ttk.Label(db_body, text="聊天库", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=5)
        ttk.Label(db_body, text=str(DB), style="Hint.TLabel").grid(
            row=0, column=1, sticky="w", padx=(12, 0), pady=5)
        ttk.Label(db_body, text="解密口令", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=5)
        pw_entry = ttk.Entry(db_body, show="*")
        pw_entry.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=5)
        ttk.Label(db_body,
                  text=("已保存口令（DPAPI 加密，仅本机当前用户可解）；留空表示不修改"
                        if has_key else "尚未保存口令，首次同步前必须填写"),
                  style="Hint.TLabel", justify="left", wraplength=520).grid(
            row=2, column=1, sticky="w", padx=(12, 0), pady=(2, 0))

        # ---------- 底部：深色模式 + 按钮 ----------
        foot = ttk.Frame(shell, style="Page.TFrame")
        foot.pack(fill="x", pady=(2, 0))

        dark_var = tk.BooleanVar(value=self.theme == "dark")
        p0 = THEMES[self.theme]
        dark_check = tk.Checkbutton(foot, text="深色模式", variable=dark_var,
                                    command=lambda: on_toggle(), cursor="hand2",
                                    bg=p0["bg"], fg=p0["text"],
                                    activebackground=p0["bg"], activeforeground=p0["text"],
                                    selectcolor=p0["field"], bd=0, highlightthickness=0,
                                    font=("Microsoft YaHei UI", 9))
        dark_check.pack(side="left")

        def on_toggle() -> None:
            t = "dark" if dark_var.get() else "light"
            p = THEMES[t]
            _apply_ui_style(style, t)
            win.configure(bg=p["bg"])
            dark_check.config(bg=p["bg"], fg=p["text"], activebackground=p["bg"],
                              activeforeground=p["text"], selectcolor=p["field"])
            sync_listbox_colors(t)
            self.set_theme(t)                 # 控制台主窗口同步换肤
            _write_section("ui", {"theme": t})

        def save() -> None:
            base = base_entry.get().strip() or cfg["base_url"]
            model = model_entry.get().strip() or cfg["model"]
            key = key_entry.get().strip()
            local = base.startswith(("http://127.0.0.1", "http://localhost"))
            if local and not key:
                key = "ollama"          # 本地 Ollama 不校验，但核心要求非空
            enabled = bool(base and key)
            _write_section("ai", {
                "base_url": base, "model": model, "api_key": key,
                "enabled": enabled, "review": enabled,
            })
            vm = vision.get().strip()
            _write_section("snap", {"vision_model": "" if vm == "（不使用）" else vm})
            pw = pw_entry.get().strip()
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
            win.destroy()

        ttk.Button(foot, text="保存", style="Accent.TButton", command=save).pack(side="right")
        ttk.Button(foot, text="取消", command=win.destroy).pack(side="right", padx=8)


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
