"""Chat2Todo 控制台：一个窗口管所有事（tkinter，标准库实现）。

功能：
- 导入文件（txt / JSON 导出 → 入库 → AI 复核，零配置）
- 立即同步（内置引擎只读 QQ 库 → 入库 → AI 复核，不再需要 Docker），日志实时滚动
- 打开看板 / 截图待办 / 目录监视开关
- 设置：Ollama 地址与模型、口令（DPAPI 加密保存）

设计原则：所有重活都交给子进程，界面永远不阻塞；出错只提示，不崩溃。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SRC = HERE.parents[1] / "src"           # 仓库源码（src 布局，免 pip install 直接跑）
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))       # 同目录的 c2t_pages 包（按页拆分）
from c2t_pages import about, ai, data, general, home, snap, todo, trash  # noqa: E402
ROOT = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Chat2Todo"
CONFIG = ROOT / "c2t.toml"
DB = ROOT / "qq.db"
PY = sys.executable


def _tools_dir() -> Path:
    """随包的 tools 目录：frozen 时在 _MEIPASS 下，开发态是仓库 tools/。

    打包后 console.py 的 __file__ 落在 _MEIPASS 根，``HERE.parent`` 会指到
    解包目录的父级，snap_todo / *.bat 全都找不到。这里统一解析真实位置。
    """
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", "") or str(Path(sys.executable).parent)
        return Path(base) / "tools"
    return HERE.parent

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


def _load_raw() -> dict:
    """读 c2t.toml 原始内容（不合并默认值）；文件不存在或解析失败返回空。"""
    if not CONFIG.exists():
        return {}
    try:
        from chat2todo.config import _read

        return _read(str(CONFIG))
    except Exception:  # noqa: BLE001
        return {}


def _read_toml_key(section_name: str, key: str) -> str:
    """从 c2t.toml 读某段某键的值；不存在返回空串。"""
    val = (_load_raw().get(section_name) or {}).get(key, "")
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


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


# ---------- 数据库一键备份 / 恢复（模块级纯逻辑，可单测）----------
BACKUP_KEEP = 5                    # 只留最近 5 份备份


def _backups_dir() -> Path:
    d = ROOT / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _list_backups() -> list:
    """全部备份（新的在前）；按文件名排序即可，名字里带时间戳。"""
    try:
        return sorted(_backups_dir().glob("qq-*.db"),
                      key=lambda f: f.name, reverse=True)
    except Exception:  # noqa: BLE001
        return []


def _prune_backups(keep: int = BACKUP_KEEP) -> list:
    """删掉超出保留数的旧备份，返回被删的文件名。"""
    removed = []
    for f in _list_backups()[keep:]:
        try:
            f.unlink()
            removed.append(f.name)
        except OSError:
            pass
    return removed


def backup_db(src: Path = DB) -> Path:
    """把数据库复制成带时间戳的备份，顺手剪枝到最近 5 份。返回备份路径。"""
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = _backups_dir() / f"qq-{stamp}.db"
    shutil.copy2(src, dst)
    _prune_backups()
    return dst


def restore_db(bak: Path, dst: Path = DB) -> Path:
    """用备份覆盖当前数据库；覆盖前先把当前库自动备份一份（可再恢复回去）。

    返回兜底备份的路径。数据库被占用时抛 PermissionError，由界面层提示。
    """
    bak = Path(bak)
    dst = Path(dst)
    safety = backup_db(dst)          # 恢复前先自保一份
    shutil.copy2(bak, dst)
    return safety


def _read_cfg() -> dict:
    """读 [ai] 段；文件里没写的键保持设置页默认值。"""
    out = {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:14b",
           "api_key": "", "enabled": False}
    ai = _load_raw().get("ai") or {}
    for k in out:
        v = ai.get(k)
        if v is not None and v != "":
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
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(f"'{x}'" for x in v) + "]"
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


def _autostart_on() -> bool:
    """看板启动项是否存在，作为「开机自启」的开关状态。"""
    import os
    from pathlib import Path

    startup = Path(os.environ.get("APPDATA", "")) / (
        "Microsoft/Windows/Start Menu/Programs/Startup")
    return (startup / "Chat2Todo 看板.lnk").exists()


def _set_autostart(on: bool) -> tuple:
    """创建 / 删除两个启动项快捷方式。返回 (成功, 提示)。"""
    import os
    import subprocess
    from pathlib import Path

    startup = Path(os.environ.get("APPDATA", "")) / (
        "Microsoft/Windows/Start Menu/Programs/Startup")
    dash = startup / "Chat2Todo 看板.lnk"
    snap = startup / "Chat2Todo 截图热键.lnk"
    if not on:
        for p_ in (dash, snap):
            try:
                p_.unlink(missing_ok=True)
            except OSError as exc:
                return False, f"删除启动项失败：{exc}"
        return True, "已关闭开机自启"

    dash_bat = _tools_dir() / "qq_db_export" / "dash_autostart.bat"
    snap_bat = _tools_dir() / "snap_todo" / "snap_autostart.bat"
    if getattr(sys, "frozen", False):
        # exe 场景下 bat 会去调系统 python，找不到打包进来的 chat2todo，必须直指 exe 自身
        exe = str(sys.executable)
        snap_py = str(_tools_dir() / "snap_todo" / "snap.py")
        ps = (
            "$w=New-Object -ComObject WScript.Shell;"
            f"$a=$w.CreateShortcut('{dash}');$a.TargetPath='{exe}';"
            "$a.Arguments='dash --interval 30';$a.WindowStyle=7;$a.Save();"
            f"$b=$w.CreateShortcut('{snap}');$b.TargetPath='{exe}';"
            f"$b.Arguments='\"{snap_py}\" --hotkey --db \"{DB}\"';$b.WindowStyle=7;$b.Save()"
        )
    else:
        ps = (
            "$w=New-Object -ComObject WScript.Shell;"
            f"$a=$w.CreateShortcut('{dash}');$a.TargetPath='{dash_bat}';$a.WindowStyle=7;$a.Save();"
            f"$b=$w.CreateShortcut('{snap}');$b.TargetPath='{snap_bat}';$b.WindowStyle=7;$b.Save()"
        )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        return False, f"创建启动项失败：{(r.stderr or '').strip()[:120]}"
    return True, "已开启开机自启（看板 + 截图热键）"


def _read_ui(key: str, default: str = "") -> str:
    """读 [ui] 某个键；必须显式传配置路径，否则读到的永远是默认值。"""
    try:
        from chat2todo.config import load

        cfg = load(str(CONFIG) if CONFIG.exists() else None) or {}
        return str((cfg.get("ui") or {}).get(key, default))
    except Exception:  # noqa: BLE001
        return default


def _read_ui_flag(key: str, default: bool = True) -> bool:
    """读 [ui] 里的布尔开关（border / clock 这类）。"""
    val = _read_ui(key, "true" if default else "false").strip().lower()
    return val not in ("false", "0", "no", "off")


def _save_ui_key(key: str, value, app, label: str = "") -> None:
    """写 [ui] 单个键；看板会在 2 秒内热重载，无需重启。"""
    _write_section("ui", {key: value})
    app.write(f"{label or key} 已改为 {value}（看板会自动刷新）\n")


def _save_ui_accent(color: str, app) -> None:
    """写入 [ui] accent 并提示。"""
    _write_section("ui", {"accent": color})
    app.write(f"强调色已改为 {color}（看板会自动刷新）\n")


def _read_snap_refine() -> str:
    """[snap] ai_refine，默认开启。"""
    try:
        from chat2todo.config import load

        return str((load(str(CONFIG) if CONFIG.exists() else None).get("snap") or {})
                   .get("ai_refine", True))
    except Exception:  # noqa: BLE001
        return "True"


def _read_filter(key: str) -> list:
    """读 [filter] 段的列表（去重、去空）。"""
    try:
        from chat2todo.config import load

        # 注意：config.load() 的默认查找路径不含 %USERPROFILE%\Chat2Todo\c2t.toml，
        # 必须显式传路径，否则读到的永远是默认值。
        cfg = load(str(CONFIG) if CONFIG.exists() else None).get("filter") or {}
        val = cfg.get(key) or []
        if isinstance(val, str):
            val = _split_ids(val)
        out = []
        for v in val:
            v = str(v).strip()
            if v and v not in out:
                out.append(v)
        return out
    except Exception:  # noqa: BLE001
        return []


def _split_ids(text: str) -> list:
    """逗号（中英文）/空格/分号都能当分隔符。"""
    out = []
    for part in str(text).replace("，", ",").replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def _read_chat_names() -> dict:
    """读库里的 群号 → 群名 映射（db-import 时顺带从 QQ 库刷新）；读不到返回空。

    群名只用于展示：查不到名字的号码（比如机器人 QQ 号）保持原样显示。
    """
    import sqlite3

    if not DB.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT chat_id, name FROM chat_names").fetchall()
        finally:
            conn.close()
        return {str(k): str(v) for k, v in rows if str(v).strip()}
    except Exception:  # noqa: BLE001 - 老库可能还没有 chat_names 表
        return {}


def _disp_width(text: str) -> int:
    """显示宽度：全角字符按 2 算，避免含群名的标签排版被低估。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _read_hotkey(section: str = "dash", key: str = "hotkey",
                 default: str = "ctrl+alt+d") -> str:
    """读某个段里的快捷键配置（[dash] hotkey 或 [snap] hotkey）。"""
    try:
        from chat2todo.config import load

        cfg = load(str(CONFIG) if CONFIG.exists() else None)
        return (cfg.get(section) or {}).get(key) or default
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
        # 默认 820x580 撑不住现在的 4 张卡（新增 + 搜索 + 列表 + 操作两行），
        # 内容总高度约 520px，加上标题栏和 padding，留一点余量到 620。
        self.geometry("960x640")
        self.minsize(820, 600)

        # 给外部自动化一个抓手：启动时把顶层 hwnd 写到指定文件，
        # 便于脚本 raise 窗口 / 截图 / 关闭。
        hwnd_file = os.environ.get("C2T_HWND_FILE")
        if hwnd_file:
            try:
                import ctypes

                top = ctypes.windll.user32.GetParent(self.winfo_id())
                if not top:
                    top = self.winfo_id()
                with open(hwnd_file, "w") as f:
                    f.write(str(int(top)))
            except Exception:
                pass

        self.syncing = False
        self.watch_proc: subprocess.Popen | None = None
        self.snap_proc: subprocess.Popen | None = None
        self.theme = _read_theme()

        # 页面模块只依赖 app 与这个 ctx；函数仍在本模块取值，测试替换 CONFIG 依旧生效
        self._ctx = SimpleNamespace(
            read_cfg=_read_cfg,
            write_section=_write_section,
            read_ui=_read_ui,
            read_ui_flag=_read_ui_flag,
            save_ui_key=_save_ui_key,
            save_ui_accent=_save_ui_accent,
            autostart_on=_autostart_on,
            set_autostart=_set_autostart,
            read_snap_model=_read_snap_model,
            read_snap_refine=_read_snap_refine,
            read_filter=_read_filter,
            read_chat_names=_read_chat_names,
            read_hotkey=_read_hotkey,
            ai_presets=AI_PRESETS,
            db=DB,
            root=ROOT,
        )

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

    def _chip_card(self, p: dict, parent, icon: str, title: str, sub: str,
                   values: list, placeholder: str, names: dict | None = None):
        """列表式编辑器：已有项做成可删除的胶囊，输入框回车/点添加；重复项自动拦住。

        names 传入 群号→群名 映射时，胶囊显示「群名（号码）」；查不到名字的
        号码（机器人 QQ 号等）保持原样。内部仍按原始号码存取。
        """
        outer = tk.Frame(parent, bg=p["card"], highlightbackground=p["border"],
                         highlightthickness=1)
        outer.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(outer, bg=p["card"])
        inner.pack(fill="x", padx=14, pady=12)

        head = tk.Frame(inner, bg=p["card"])
        head.pack(fill="x")
        tk.Label(head, text=icon, bg=p["card"], fg=p["title"],
                 font=("Segoe UI Emoji", 13)).pack(side="left", padx=(0, 12))
        texts = tk.Frame(head, bg=p["card"])
        texts.pack(side="left", fill="x", expand=True)
        tk.Label(texts, text=title, bg=p["card"], fg=p["text"], anchor="w",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        if sub:
            tk.Label(texts, text=sub, bg=p["card"], fg=p["muted"], anchor="w",
                     justify="left", wraplength=520,
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(1, 0))

        state = {"items": [], "names": dict(names or {})}
        for v in values:
            v = str(v).strip()
            if v and v not in state["items"]:
                state["items"].append(v)

        def _label(item: str) -> str:
            gname = state["names"].get(item, "")
            return f"{gname}（{item}）" if gname else item

        chips = tk.Frame(inner, bg=p["card"])
        chips.pack(fill="x", pady=(8, 0))
        hint = tk.Label(inner, text="", bg=p["card"], fg=p["muted"],
                        font=("Microsoft YaHei UI", 8))
        hint.pack(anchor="w", pady=(4, 0))

        def remove(item: str) -> None:
            state["items"].remove(item)
            hint.config(text=f"已移除 {item}", fg=p["muted"])
            redraw()

        combo_var = tk.StringVar()

        def _combo_values() -> list:
            """还不在名单里的已知群：群名（号码），按群名排序。"""
            return [f"{name}（{cid}）" if name else cid
                    for cid, name in sorted(state["names"].items(),
                                            key=lambda kv: kv[1] or kv[0])
                    if cid not in state["items"]]

        def _sync_combo() -> None:
            vals = _combo_values()
            combo.config(values=vals)
            if vals:
                if not combo.winfo_ismapped():
                    combo.pack(side="right", ipady=3)
            else:
                combo.pack_forget()

        def redraw() -> None:
            for w in chips.winfo_children():
                w.destroy()
            if not state["items"]:
                tk.Label(chips, text="（还没有条目）", bg=p["card"], fg=p["muted"],
                         font=("Microsoft YaHei UI", 8)).pack(anchor="w")
                _sync_combo()
                return
            row = None
            used = 0
            for item in state["items"]:
                label = _label(item)
                width = _disp_width(label) * 9 + 54
                if row is None or used + width > 560:
                    row = tk.Frame(chips, bg=p["card"])
                    row.pack(fill="x", pady=2)
                    used = 0
                chip = tk.Frame(row, bg=p["field"], highlightbackground=p["border"],
                                highlightthickness=1)
                chip.pack(side="left", padx=(0, 6))
                tk.Label(chip, text=label, bg=p["field"], fg=p["text"],
                         font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(8, 2),
                                                              pady=2)
                tk.Button(chip, text="×", command=lambda x=item: remove(x), bd=0,
                          bg=p["field"], fg=p["muted"], activebackground=p["field"],
                          activeforeground=p["err"], cursor="hand2",
                          font=("Microsoft YaHei UI", 9), padx=4).pack(side="left")
                used += width
            _sync_combo()

        bar = tk.Frame(inner, bg=p["card"])
        bar.pack(fill="x", pady=(6, 0))
        ent = tk.Entry(bar, width=22, bg=p["field"], fg=p["field_text"],
                       insertbackground=p["field_text"], bd=0, relief="flat",
                       highlightthickness=1, highlightbackground=p["border"],
                       highlightcolor=p["accent"], font=("Microsoft YaHei UI", 9))
        ent.pack(side="left", ipady=3)

        def add(_evt=None) -> None:
            v = ent.get().strip()
            if not v:
                return
            if v in state["items"]:
                hint.config(text=f"「{v}」已经在列表里，不会重复添加", fg=p["err"])
                return
            state["items"].append(v)
            ent.delete(0, "end")
            hint.config(text=f"已添加 {v}", fg=p["ok"])
            redraw()

        ent.bind("<Return>", add)
        tk.Button(bar, text="添加", command=add, bg=p["btn"], fg=p["btn_text"], bd=0,
                  padx=14, pady=4, cursor="hand2",
                  font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(8, 0))
        tk.Label(bar, text=placeholder, bg=p["card"], fg=p["muted"],
                 font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(10, 0))

        # 按群名搜索添加：下拉列出还不在名单里的已知群（可敲字过滤），选中即加入
        combo = ttk.Combobox(bar, textvariable=combo_var, width=20,
                             font=("Microsoft YaHei UI", 9))

        def _combo_type(_evt=None) -> None:
            typed = combo_var.get().strip().lower()
            vals = _combo_values()
            if typed:
                vals = [v for v in vals if typed in v.lower()]
            combo.config(values=vals)

        def _combo_pick(_evt=None) -> None:
            sel = combo_var.get().strip()
            if not sel:
                return
            if sel.endswith("）") and "（" in sel:
                cid = sel[sel.rfind("（") + 1:-1]
            else:
                cid = sel                    # 没名字的条目本身就是号码
            combo_var.set("")
            ent.delete(0, "end")
            ent.insert(0, cid)
            add()                            # 走统一入口：重复会自动拦住

        combo.bind("<KeyRelease>", _combo_type)
        combo.bind("<<ComboboxSelected>>", _combo_pick)
        redraw()
        state["redraw"] = redraw
        return state

    # ---------- 单窗口：侧栏 + 分页 ----------
    NAV = [
        ("console", "◈", "控制台"),
        ("todo", "☑", "待办"),
        ("data", "⛁", "数据"),
        ("general", "🏠", "常规"),
        ("ai", "✨", "AI 服务"),
        ("snap", "📷", "截图识别"),
        ("trash", "🗑", "最近删除"),
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
        self._build_page_todo()
        self._build_page_data()
        self._build_page_general()
        self._build_page_ai()
        self._build_page_snap()
        self._build_page_trash()
        self._build_page_about()

        self._show_page(getattr(self, "_current_page", "console"))

    def _new_page(self) -> tk.Frame:
        """可滚动页面：内容超出窗口时出现滚动条，内容区宽度跟随窗口。"""
        p = self._pal()
        holder = tk.Frame(self.body, bg=p["bg"])
        canvas = tk.Canvas(holder, bg=p["bg"], highlightthickness=0)
        vbar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        hbar = ttk.Scrollbar(holder, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        vbar.pack(side="right", fill="y")
        hbar.pack(side="bottom", fill="x")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=p["bg"])
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        def _on_inner_cfg(_e):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_cfg(e):
            # 内容区宽度取「画布宽度」和「内容自然宽度」的较大者：
            # 内容窄时撑满画布（保留 fill=x 布局），内容宽时保留自然宽度 → 横向滚动条生效。
            inner.update_idletasks()
            req_w = inner.winfo_reqwidth()
            canvas.itemconfigure(win, width=max(e.width, req_w))

        inner.bind("<Configure>", _on_inner_cfg)
        canvas.bind("<Configure>", _on_canvas_cfg)

        def on_wheel(e) -> None:
            if e.state & 0x0001:  # Shift 按下 → 横向滚
                canvas.xview_scroll(int(-1 * (e.delta / 120)), "units")
            else:
                canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", on_wheel, add="+")
        inner._holder = holder  # type: ignore[attr-defined]
        inner._canvas = canvas  # type: ignore[attr-defined]
        return inner

    def _show_page(self, key: str) -> None:
        p = self._pal()
        self._current_page = key
        for frame in self._pages.values():
            holder = getattr(frame, "_holder", frame)
            holder.pack_forget()
        holder = getattr(self._pages[key], "_holder", self._pages[key])
        holder.pack(fill="both", expand=True)
        label = next(n for n in self.NAV if n[0] == key)
        self.page_title.config(text=label[2])
        sub = {
            "console": "同步、看板与运行日志",
            "todo": "待办的搜索、完成、删除与判定报告",
            "data": "原文检索、导出、统计与清理",
            "general": "外观、数据库口令与定时同步",
            "ai": "服务商、密钥与连接测试",
            "snap": "截图待办使用的识别引擎",
            "trash": "回收站与保留天数",
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
        home.build(self, self._ctx)

    # ---------- 页：待办 ----------
    def _build_page_todo(self) -> None:
        todo.build(self, self._ctx)

    # ---------- 页：数据 ----------
    def _build_page_data(self) -> None:
        data.build(self, self._ctx)

    # ---------- 页：常规 ----------
    def _build_page_general(self) -> None:
        general.build(self, self._ctx)

    def _refresh_chat_names(self) -> None:
        """后台补抓群名：写库后回到主线程刷新两张过滤卡片的显示。"""
        try:
            from chat2todo.config import load
            from chat2todo.core import Store
            from chat2todo.sources.db import fetch_group_names

            names = fetch_group_names(load(str(CONFIG)))
            if not names:
                return
            store = Store(str(DB))
            try:
                store.set_chat_names(names)
            finally:
                store.close()
            fresh = _read_chat_names()
            if not fresh:
                return

            def apply() -> None:
                for st in (self.ignore_state, self.star_state):
                    st["names"].update(fresh)
                    st["redraw"]()
                self.write(f"已刷新群名 {len(fresh)} 条，列表里的群号已换成群名\n")

            self.after(0, apply)
        except Exception:  # noqa: BLE001 - 群名只是展示，失败静默
            return

    # ---------- 页：AI ----------
    def _build_page_ai(self) -> None:
        ai.build(self, self._ctx)

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
        snap.build(self, self._ctx)

    # ---------- 页：最近删除 ----------
    def _build_page_trash(self) -> None:
        trash.build(self, self._ctx)

    def _read_trash_days(self) -> int:
        try:
            from chat2todo.config import load

            return int((load(str(CONFIG)).get("ui") or {}).get("trash_days", 30))
        except Exception:  # noqa: BLE001
            return 30

    def _refresh_trash(self) -> None:
        """把回收站内容读进列表（行格式：日期 · 内容）。"""
        box = getattr(self, "trash_box", None)
        if box is None:
            return
        box.delete(0, "end")
        self._trash_rows = []
        keyword = ""
        ent = getattr(self, "trash_search", None)
        if ent is not None:
            keyword = ent.get().strip().lower()
        try:
            from chat2todo.core.store import Store

            st = Store(str(DB))
            try:
                rows = st.list_trash()
            finally:
                st.close()
        except Exception as exc:  # noqa: BLE001
            self.write(f"读取回收站失败：{exc}\n")
            return
        if keyword:
            rows = [r for r in rows
                    if keyword in (r.get("text") or "").lower()
                    or keyword in (r.get("sender") or "").lower()
                    or keyword in (r.get("deleted_at") or "").lower()]
        self._trash_rows = rows
        for r in rows:
            text = (r.get("text") or "").replace("\n", " ")
            if len(text) > 40:
                text = text[:40] + "…"
            box.insert("end", f"{r.get('deleted_at', '')}  {text}")

    def on_trash_select_all(self) -> None:
        """全选回收站列表（再点一次＝全不选）。"""
        if not hasattr(self, "trash_box"):
            return
        n = self.trash_box.size()
        if n and len(self.trash_box.curselection()) == n:
            self.trash_box.selection_clear(0, "end")
        else:
            self.trash_box.selection_set(0, "end")

    def on_restore_trash(self) -> None:
        sel = self.trash_box.curselection() if hasattr(self, "trash_box") else ()
        if not sel:
            messagebox.showinfo("最近删除", "先在列表里选一条")
            return
        rows = [self._trash_rows[i] for i in sel if i < len(self._trash_rows)]
        done = 0
        try:
            from chat2todo.core.store import Store

            st = Store(str(DB))
            try:
                for row in rows:
                    if st.restore(int(row["id"])):
                        done += 1
            finally:
                st.close()
        except Exception as exc:  # noqa: BLE001
            self.write(f"还原失败：{exc}\n")
            return
        self.write(f"已还原 {done} 条\n")
        self._refresh_trash()

    def on_clear_trash(self) -> None:
        if not messagebox.askyesno("清空回收站", "确定永久删除回收站里的全部记录？"):
            return
        try:
            from chat2todo.core.store import Store

            st = Store(str(DB))
            try:
                n = st.clear_trash()
            finally:
                st.close()
        except Exception as exc:  # noqa: BLE001
            self.write(f"清空失败：{exc}\n")
            return
        self.write(f"已清空回收站（{n} 条）\n")
        self._refresh_trash()

    def on_save_trash_days(self) -> None:
        raw = self.trash_days.get().strip()
        try:
            days = max(0, int(raw))
        except ValueError:
            messagebox.showerror("最近删除", "保留天数要填数字（0 表示永久保留）")
            return
        _write_section("ui", {"trash_days": days})
        self.write(f"回收站保留天数已设为 {days} 天（0=永久）\n")

    def _build_page_about(self) -> None:
        about.build(self, self._ctx)

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
        # 外观：不透明度（留着 accent 由色块按钮即时写入，不在这里覆盖）
        try:
            op = float(self.op_entry.get().strip())
            op = max(0.35, min(1.0, op))
        except (AttributeError, TypeError, ValueError):
            op = None
        ui_vals = {}
        if op is not None:
            ui_vals["opacity"] = round(op, 2)
        for attr, key, lo, hi in (("width_entry", "width", 280, 560),
                                  ("radius_entry", "radius", 0, 30)):
            try:
                v = int(getattr(self, attr).get().strip())
                ui_vals[key] = max(lo, min(hi, v))
            except (AttributeError, TypeError, ValueError):
                pass
        if ui_vals:
            _write_section("ui", ui_vals)
            self.write(f"不透明度已保存为 {round(op, 2)}（看板会自动刷新）\n")
        pw = self.pw_entry.get().strip()
        if pw:
            try:
                from chat2todo.core import keybox

                keybox.save_key(pw)
                self.write("口令已加密保存（DPAPI，仅本机当前用户可解）\n")
            except Exception as exc:  # noqa: BLE001
                self.write(f"口令保存失败：{exc}\n")
        _write_section("filter", {
            "ignore_groups": list(self.ignore_state["items"]),
            "star_groups": list(self.star_state["items"]),
        })
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
            self.stats.config(text="还没有数据，点「导入文件」或「立即同步」")
        else:
            self.stats.config(
                text=f"消息 {s['messages']} · 待办 {s['todos']} · 待处理 {s['pending']} · 待定 {s['unconfirmed']}"
            )

    # ---------- 动作 ----------
    def _stream(self, title: str, cmd: list[str], on_done=None) -> None:
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
                if on_done:
                    self.after(0, lambda: on_done())

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

        if getattr(sys, "frozen", False):
            # exe 场景直接让计划任务跑 exe 自身的 CLI，不依赖系统 python 与 bat
            tr = (f'"{sys.executable}" -u -m chat2todo --config "{CONFIG}" '
                  f'db-import --days 1 --db "{DB}"')
        else:
            bat = _tools_dir() / "qq_db_export" / "后台同步.bat"
            if not bat.exists():
                messagebox.showerror("定时同步", f"找不到 {bat.name}，请检查安装是否完整")
                return
            tr = f'cmd /c "{bat}"'
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", task, "/SC", "HOURLY", "/MO", "2",
             "/TR", tr, "/F"],
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
            cmd = [PY, "-u", "-m", "chat2todo", "--config", str(CONFIG),
               "db-import", "--days", "7", "--db", str(DB)]
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
                self.after(0, lambda: self.write(
                    "✅ 同步完成，看板 5 秒内自动刷新新待办\n"))

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
        before_empty = not _stat()  # 导入前是否空库 → 首次导入信号
        self._stream("导入文件", [PY, "-m", "chat2todo", "import", path, "--db", str(DB)],
                     on_done=lambda: self._maybe_show_plugin_hint(before_empty))

    def _maybe_show_plugin_hint(self, before_empty: bool) -> None:
        """首次导入成功后弹引导卡：告诉用户有截图待办、DB 直读等进阶能力。"""
        if not before_empty:
            return  # 不是首次，跳过
        s = _stat()
        if not s or s.get("todos", 0) == 0:
            return  # 这次导入没产出，不算成功
        messagebox.showinfo(
            "✅ 导入完成",
            f"已入库 {s['messages']} 条消息，抽出 {s['todos']} 条待办。\n\n"
            f"想更省事？试试：\n"
            f"• 截图待办 — 点控制台「截图待办」按钮，Win+Shift+S 框一下自动识别\n"
            f"• 定时光同步 — 进「设置」→「定时同步」，每 2 小时自动从 QQ 库抓取\n"
            f"• DB 直读进阶 — 进「设置」填一次 QQ 口令，以后一键同步"
        )

    def _unmarked_count(self) -> int:
        """未标记来源群的消息条数；读不到当 0。"""
        try:
            from chat2todo.core import Store

            store = Store(str(DB))
            try:
                return store.unmarked_counts()["messages"]
            finally:
                store.close()
        except Exception:  # noqa: BLE001
            return 0

    def _weekly_stats(self) -> dict:
        """近 7 天摘要；库读不到时返回空。"""
        try:
            from chat2todo.core import Store

            store = Store(str(DB))
            try:
                return store.weekly_stats()
            finally:
                store.close()
        except Exception:  # noqa: BLE001
            return {}

    def _calibration_stats(self) -> dict:
        """校准反馈可观测数据；库读不到时返回空。"""
        try:
            from chat2todo.core import Store

            store = Store(str(DB))
            try:
                return store.calibration_insights()
            finally:
                store.close()
        except Exception:  # noqa: BLE001
            return {}

    def on_export_calibration(self) -> None:
        """把当前校准档案导成 JSON（项目专属 schema，可跨设备/跨版本迁移）。"""
        from chat2todo.core import Store
        from datetime import datetime

        path = filedialog.asksaveasfilename(
            title="导出校准档案",
            defaultextension=".json",
            filetypes=[("校准档案", "*.json"), ("所有文件", "*.*")],
            initialfile=f"c2t-calibration-{datetime.now().strftime('%Y%m%d')}.json",
        )
        if not path:
            return
        try:
            store = Store(str(DB))
            try:
                n = store.export_calibration(path)
            finally:
                store.close()
            messagebox.showinfo("导出成功", f"{n} 条校准反馈已导出到：\n{path}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导出失败", str(exc))

    def on_import_calibration(self) -> None:
        """从 JSON 导入校准档案（schema 版本不匹配会拒绝）。"""
        from chat2todo.core import Store

        path = filedialog.askopenfilename(
            title="导入校准档案",
            filetypes=[("校准档案", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            store = Store(str(DB))
            try:
                result = store.import_calibration(path)
            finally:
                store.close()
            msg = (f"导入完成：新增 {result['inserted']} 条"
                   f"（schema 版本不匹配或已存在的已跳过）")
            messagebox.showinfo("导入成功", msg)
        except ValueError as exc:
            messagebox.showwarning("跳过导入", str(exc))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导入失败", str(exc))

    def on_purge_unmarked(self) -> None:
        n = self._unmarked_count()
        if not n:
            return
        if not messagebox.askyesno(
                "清理未标记消息",
                f"将删除 {n} 条没有来源群的老消息（txt 导入产生）。\n"
                "已抽出的待办会保留。确定继续？"):
            return
        self._stream("清理未标记消息",
                     [PY, "-m", "chat2todo", "purge-unmarked", "--db", str(DB)])

    # ---------- 数据库备份 / 恢复 ----------
    def on_backup_db(self) -> None:
        if not DB.exists():
            messagebox.showinfo("备份数据库", f"还没有数据库：\n{DB}")
            return
        try:
            dst = backup_db()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("备份数据库", f"备份失败：{exc}")
            return
        self.write(f"已备份数据库 → backups/{dst.name}（只留最近 {BACKUP_KEEP} 份）\n")
        messagebox.showinfo("备份数据库", f"已备份到：\n{dst}")

    def on_restore_db(self) -> None:
        backs = _list_backups()
        if not backs:
            messagebox.showinfo("恢复数据库", "还没有备份，先点「备份」存一份。")
            return
        p = self._pal()
        win = tk.Toplevel(self)
        win.title("恢复数据库")
        win.configure(bg=p["bg"])
        win.transient(self)
        tk.Label(win, text="选择要恢复的备份（恢复前会先把当前库自动备份一份）",
                 bg=p["bg"], fg=p["text"], anchor="w",
                 font=("Microsoft YaHei UI", 9)).pack(fill="x", padx=14, pady=(12, 6))
        lb = tk.Listbox(win, height=min(8, len(backs)), width=46, bd=0,
                        bg=p["field"], fg=p["field_text"],
                        selectbackground=p["accent"], selectforeground=p["accent_fg"],
                        highlightthickness=1, highlightbackground=p["border"],
                        font=("Consolas", 9))
        for f in backs:
            lb.insert("end", f.name)
        lb.selection_set(0)
        lb.pack(padx=14, fill="x")

        def _do() -> None:
            sel = lb.curselection()
            if not sel:
                return
            src = backs[sel[0]]
            if not messagebox.askyesno(
                    "恢复数据库",
                    f"用 {src.name} 覆盖当前数据库？\n"
                    "（当前库会先自动备份一份；恢复后需重启看板生效）",
                    parent=win):
                return
            try:
                safety = restore_db(src)
            except PermissionError:
                messagebox.showerror(
                    "恢复数据库", "恢复失败：数据库正被看板占用。\n请先关闭看板再试。",
                    parent=win)
                return
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("恢复数据库", f"恢复失败：{exc}", parent=win)
                return
            win.destroy()
            self.write(f"已用 {src.name} 覆盖当前数据库；原库存为 {safety.name}\n")
            self.refresh_stats()
            messagebox.showinfo("恢复数据库", "恢复完成。请重启看板使其生效。")

        tk.Button(win, text="恢复所选备份", command=_do, bg=p["accent"],
                  fg=p["accent_fg"], bd=0, padx=16, pady=6, cursor="hand2",
                  font=("Microsoft YaHei UI", 9, "bold")).pack(pady=12)
        lb.bind("<Double-Button-1>", lambda _e: _do())

    def on_snap(self) -> None:
        """截图待办：独立小窗进程，不阻塞控制台（自带确认弹窗）。"""
        snap = _tools_dir() / "snap_todo" / "snap.py"
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
        snap = _tools_dir() / "snap_todo" / "snap.py"
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


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    page = ""
    for i, a in enumerate(argv):
        if a == "--page" and i + 1 < len(argv):
            page = argv[i + 1]
        elif a.startswith("--page="):
            page = a.split("=", 1)[1]
    try:
        app = Console()
    except tk.TclError as exc:
        print(f"无法创建窗口：{exc}", file=sys.stderr)
        return 1
    app.write("欢迎使用 Chat2Todo 控制台。\n点「导入文件」选一份导出的聊天记录开始，或用「立即同步」直读 QQ 数据库。\n")
    if page:
        try:
            app._show_page(page)
        except Exception:  # noqa: BLE001
            pass
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
