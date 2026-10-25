"""Chat2Todo 控制台：一个窗口管所有事（tkinter，标准库实现）。

功能：
- 立即同步（解密→入库→AI 复核），日志实时滚动
- 打开看板 / 导入 txt / 目录监视开关
- 设置：Ollama 地址与模型、抓取天数、AI 复核上限、阈值、口令（DPAPI 加密保存）

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
ROOT = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Chat2Todo"
CONFIG = ROOT / "c2t.toml"
DB = ROOT / "qq.db"
SYNC = HERE / "sync.py"
FROZEN_PY = sys.executable


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
    out = {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:14b"}
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


def _write_cfg(cfg: dict) -> None:
    """只改 [ai] 段里的 base_url / model，其余内容原样保留。"""
    ROOT.mkdir(parents=True, exist_ok=True)
    lines = CONFIG.read_text(encoding="utf-8-sig").splitlines() if CONFIG.exists() else []

    start = end = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if s[1:-1].strip() == "ai" and start < 0:
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
                    new_block.append(f"{k} = '{cfg[k]}'")
                    seen.add(k)
                    continue
            new_block.append(line)
        for k in cfg:
            if k not in seen:
                new_block.append(f"{k} = '{cfg[k]}'")
        lines[start + 1:end] = new_block
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("[ai]")
        for k, v in cfg.items():
            lines.append(f"{k} = '{v}'")
    CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")


class Console(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Chat2Todo 控制台")
        self.geometry("640x520")
        self.minsize(560, 440)
        self.configure(bg="#fbfbfb")

        self.syncing = False
        self.watch_proc: subprocess.Popen | None = None

        self._build_header()
        self._build_buttons()
        self._build_log()
        self.refresh_stats()

    # ---------- 界面 ----------
    def _build_header(self) -> None:
        box = tk.Frame(self, bg="#fbfbfb")
        box.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(box, text="Chat2Todo", bg="#fbfbfb", fg="#1f3a5f",
                 font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        self.stats = tk.Label(box, text="", bg="#fbfbfb", fg="#6b7280",
                              font=("Microsoft YaHei UI", 9))
        self.stats.pack(side="right")

    def _build_buttons(self) -> None:
        box = tk.Frame(self, bg="#fbfbfb")
        box.pack(fill="x", padx=10, pady=4)

        def btn(text, cmd, primary=False):
            b = tk.Button(box, text=text, command=cmd, relief="flat", cursor="hand2",
                          bg="#1f6feb" if primary else "#eef2f7",
                          fg="white" if primary else "#1f3a5f",
                          font=("Microsoft YaHei UI", 9), padx=12, pady=6)
            b.pack(side="left", padx=4, pady=2)
            return b

        self.sync_btn = btn("立即同步", self.on_sync, primary=True)
        btn("打开看板", self.on_dashboard)
        btn("导入文件", self.on_import)
        self.watch_btn = btn("开始监视", self.on_watch)
        btn("设置", self.on_settings)

    def _build_log(self) -> None:
        wrap = tk.Frame(self, bg="#fbfbfb")
        wrap.pack(fill="both", expand=True, padx=12, pady=(6, 12))
        self.log = tk.Text(wrap, bg="#111418", fg="#d7dce3", insertbackground="#d7dce3",
                           relief="flat", font=("Consolas", 9), wrap="word")
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
                                        text=True, encoding="utf-8", errors="replace", bufsize=1)
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

    def on_sync(self) -> None:
        if self.syncing:
            messagebox.showinfo("正在同步", "同步还没结束，稍等一下")
            return
        self.syncing = True
        self.sync_btn.config(state="disabled", text="同步中…")

        def worker() -> None:
            cmd = [FROZEN_PY, str(SYNC)]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace", bufsize=1)
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
            subprocess.Popen([FROZEN_PY, "-m", "chat2todo", "dash", "--db", str(DB), "--interval", "30"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            self.write("已打开看板\n")
        except Exception as exc:  # noqa: BLE001
            self.write(f"看板打开失败：{exc}\n")

    def on_import(self) -> None:
        path = filedialog.askopenfilename(title="选择聊天记录导出文件",
                                          filetypes=[("文本/导出文件", "*.txt *.mht *.html *.json"), ("全部", "*.*")])
        if not path:
            return
        self._stream("导入文件", [FROZEN_PY, "-m", "chat2todo", "import", path, "--db", str(DB)])

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
                [FROZEN_PY, "-m", "chat2todo", "watch", "--dir", str(desktop), "--interval", "5"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            self.watch_btn.config(text="停止监视")
            self.write(f"开始监视桌面：{desktop}\n")
        except Exception as exc:  # noqa: BLE001
            self.write(f"监视启动失败：{exc}\n")

    def on_settings(self) -> None:
        cfg = _read_cfg()
        win = tk.Toplevel(self)
        win.title("设置")
        win.geometry("460x300")
        win.configure(bg="#fbfbfb")
        win.transient(self)

        rows = [("Ollama 地址", "base_url"), ("模型名", "model")]
        entries: dict[str, tk.Entry] = {}
        for i, (label, key) in enumerate(rows):
            tk.Label(win, text=label, bg="#fbfbfb", font=("Microsoft YaHei UI", 9)).grid(
                row=i, column=0, sticky="e", padx=8, pady=8)
            e = tk.Entry(win, width=42, font=("Consolas", 9))
            e.insert(0, cfg[key])
            e.grid(row=i, column=1, padx=8, pady=8, sticky="w")
            entries[key] = e

        tk.Label(win, text="口令", bg="#fbfbfb", font=("Microsoft YaHei UI", 9)).grid(
            row=2, column=0, sticky="e", padx=8, pady=8)
        key_entry = tk.Entry(win, width=42, show="*", font=("Consolas", 9))
        key_entry.grid(row=2, column=1, padx=8, pady=8, sticky="w")

        def save() -> None:
            new = {k: entries[k].get().strip() or cfg[k] for k in entries}
            _write_cfg(new)
            pw = key_entry.get().strip()
            if pw:
                try:
                    subprocess.run([FROZEN_PY, "-c",
                                    "import sys,os;sys.path.insert(0,r'%s');import sync;sync.save_key(os.environ['K'])" % HERE,
                                    ], env={**os.environ, "K": pw, "PYTHONIOENCODING": "utf-8"},
                                   check=True)
                    self.write("口令已加密保存\n")
                except Exception as exc:  # noqa: BLE001
                    self.write(f"口令保存失败：{exc}\n")
            self.write("设置已保存（重启同步生效）\n")
            win.destroy()

        tk.Button(win, text="保存", command=save, bg="#1f6feb", fg="white", relief="flat",
                  font=("Microsoft YaHei UI", 9), padx=16, pady=5).grid(
            row=3, column=1, sticky="e", padx=8, pady=16)
        tk.Label(win, text="提示：地址填 http://127.0.0.1:11434/v1 即为本机 Ollama",
                 bg="#fbfbfb", fg="#6b7280", font=("Microsoft YaHei UI", 8)).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=10)


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
