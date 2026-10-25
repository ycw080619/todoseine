"""一键同步：快照 QQ 库 → 容器解密导出 → 入库 → AI 复核。

用法：
    python sync.py --save-key            # 第一次：加密保存数据库口令（DPAPI，仅本机可解）
    python sync.py                       # 之后：一条命令跑完整条链路
    python sync.py --days 3 --max-ai 40  # 只看近 3 天，最多问 40 次模型
    python sync.py --no-review           # 只导入不复核

前置：Docker Desktop 已安装；QQ 已登录过（库里得有数据）。
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parent.parent / "src"
KEY_FILE = Path.home() / "Chat2Todo" / "dbkey.dat"
PY = sys.executable


# ---------------------------------------------------------------- 口令
def _ps(cmd: str) -> str:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True, text=True,
    )
    return out.stdout.strip()


def save_key(key: str) -> None:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ps(
        "$s = ConvertTo-SecureString $env:K -AsPlainText -Force;"
        f"ConvertFrom-SecureString $s | Set-Content -Encoding ASCII '{KEY_FILE}'"
    ).strip()
    os.environ.pop("K", None)
    print(f"已加密保存口令到 {KEY_FILE}（DPAPI，仅当前用户可解）")


def load_key() -> str:
    if not KEY_FILE.exists():
        return ""
    return _ps(
        f"$s = Get-Content '{KEY_FILE}' | ConvertTo-SecureString;"
        "[Runtime.InteropServices.Marshal]::PtrToStringAuto("
        "[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))"
    )


# ---------------------------------------------------------------- 环境
def docker_ready() -> bool:
    r = subprocess.run(["docker", "info"], capture_output=True, text=True)
    return r.returncode == 0


def ensure_docker() -> bool:
    if docker_ready():
        return True
    exe = Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe")
    if exe.exists():
        print("Docker 未运行，正在启动（最多等 120 秒）…")
        subprocess.Popen([str(exe)])
        for _ in range(24):
            time.sleep(5)
            if docker_ready():
                print("Docker 就绪")
                return True
    print("Docker 不可用：请先打开 Docker Desktop")
    return False


def find_qq_db() -> Path | None:
    import winreg

    docs = []
    try:
        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as hk:
            val, _ = winreg.QueryValueEx(hk, "Personal")
            docs.append(Path(os.path.expandvars(val)))
    except OSError:
        pass
    docs.append(Path.home() / "Documents")
    for d in docs:
        for hit in (d / "Tencent Files").glob("*/nt_qq/nt_db/nt_msg.db"):
            return hit
    return None


# ---------------------------------------------------------------- 主流程
def run_step(title: str, cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> bool:
    print(f"\n▸ {title}")
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env)
    ok = proc.returncode == 0
    print(f"  {'✓' if ok else '✗'} 用时 {time.time() - t0:.0f}s")
    return ok


def start_dashboard(db_path: str) -> None:
    """同步完顺手把看板拉起来（独立进程，关掉同步窗口不影响看板）。"""
    cmd = [sys.executable, "-m", "chat2todo", "dash", "--db", db_path, "--interval", "30"]
    try:
        subprocess.Popen(
            cmd,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001 - 看板起不来不该影响同步结果
        print(f"   看板启动失败：{exc}（可双击「打开待办看板」手动打开）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path.home() / "Chat2Todo" / "qq.db"))
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--max-ai", type=int, default=120)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--save-key", action="store_true", help="交互式保存数据库口令后退出")
    ap.add_argument("--key", default="")
    ap.add_argument("--no-review", action="store_true")
    ap.add_argument("--open-dash", action="store_true", help="同步完成后自动打开看板")
    args = ap.parse_args()

    if args.save_key:
        k = getpass.getpass("请输入 QQ 数据库口令（16 位，输入时不显示）：").strip()
        if len(k) < 8:
            print("口令太短，已放弃")
            return 1
        os.environ["K"] = k
        save_key(k)
        return 0

    key = args.key or os.environ.get("NTQQ_DB_KEY", "") or load_key()
    if not key:
        print("没有数据库口令：先运行 `python sync.py --save-key` 保存一次，"
              "或设置环境变量 NTQQ_DB_KEY")
        return 1

    src = find_qq_db()
    if not src:
        print("没找到 nt_msg.db：确认 QQ 装过并登录过")
        return 1
    print(f"源库：{src}（{src.stat().st_size / 1048576:.1f} MB）")

    if not ensure_docker():
        return 1

    work = Path(tempfile.mkdtemp(prefix="qqsync_"))
    snap = work / "nt_msg.db"
    try:
        print("\n▸ 复制快照（QQ 正在写库，必须先拍快照）")
        shutil.copy2(src, snap)
        print(f"  ✓ {snap.stat().st_size / 1048576:.1f} MB")

        jsonl = work / "messages.jsonl"
        cmd = [
            "docker", "run", "--rm",
            "-e", f"NTQQ_DB_KEY={key}",
            "-e", f"DAYS={args.days}",
            "-e", "SRC=/data/nt_msg.db",
            "-v", f"{work}:/data",
            "-v", f"{HERE}:/tools:ro",
            "--dns", "114.114.114.114", "--dns", "8.8.8.8",
            "python:3.11-slim", "bash", "/tools/run.sh",
        ]
        # run.sh 会调用 export.py，需要先在 /tools 里可见（已只读挂载）
        cmd_ok = run_step("容器内解密并导出 JSONL", cmd)
        if not cmd_ok or not jsonl.exists():
            print("导出失败：检查 Docker 是否正常、口令是否仍有效")
            return 1
        print(f"  → {jsonl}（{sum(1 for _ in jsonl.open(encoding='utf-8'))} 条）")

        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC_DIR)
        if not run_step(
            "入库（规则抽取 + 语义过滤）",
            [PY, str(HERE / "pipeline.py"), str(jsonl), "--db", args.db,
             "--min-conf", "0.4", "--max-ai", "0"],
            env=env,
        ):
            return 1

        if not args.no_review and args.max_ai > 0:
            if not run_step(
                "本地 AI 复核（筛选噪声）",
                [PY, str(HERE / "review.py"), "--db", args.db,
                 "--all", "--workers", str(args.workers)],
                env=env,
            ):
                print("复核未完成，已导入的数据不受影响")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        print("\n临时文件已清理")

    print("\n✅ 同步完成")
    print(f"   数据库：{args.db}")
    if args.open_dash:
        print("   正在打开看板…")
        start_dashboard(args.db)
    else:
        print("   （加 --open-dash 可同步完直接弹看板）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
