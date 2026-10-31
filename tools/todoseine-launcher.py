"""TodoSeine 统一入口：开发和打包都用这一个脚本。

路由规则（argv 处理顺序）：
  0. 剥 leading Python flags（-u/-s/-O/-B 等）— console.py 有些调用带 -u 缓冲参数
  1. 剥到剩下 "-m todoseine" → 路由 todoseine.cli.main()
  2. args[0] 以 .py 结尾 → snap_todo 等独立脚本（frozen 跳过、开发态 exec）
  3. args[0] 是 todoseine 子命令 → CLI
  4. 其他 / 无参数 → 控制台

开发时：直接 `python todoseine-launcher.py`
打包后：todoseine.exe 本身就是入口，console.py 的 subprocess 调用
       `[sys.executable, "-u", "-m", "todoseine", "import", ...]` 会被这条规则接住。
"""

from __future__ import annotations

import sys
from pathlib import Path

# --- 路径自举 ---
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
CONSOLE_DIR = HERE / "qq_db_export"
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

# todoseine 已知子命令（cli.py build_parser 里注册的全部）
_KNOWN_SUBCOMMANDS = {
    "import", "todos", "done", "search", "show", "explain", "export", "export-md",
    "stats", "doctor", "adapters", "dash",
    "db", "db-key", "db-import", "db-backfill",
    "purge-chat", "purge-unmarked",
    "export-calibration", "import-calibration", "export-chatlab",
    "watch", "plugins", "serve",
    "mute", "unmute", "mutes",
    "backup", "wipe", "obsidian-sync",
    "desktop-export", "desktop-token",
    "plugin-status", "plugin-enable", "plugin-disable", "plugin-rollback",
    "migrate", "sbom",
    "hermes-token", "hermes-serve", "hermes-summary",
}


_USAGE = """TodoSeine 用法:
  todoseine.exe                    启动控制台 GUI
  todoseine.exe <subcommand>       运行 CLI 子命令（import / db-import / todos / stats / ...）
  todoseine.exe -h | --help        显示本帮助

子命令完整列表见: todoseine.exe doctor
""".strip()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # 规则 -1：--help / -h 早返回（避免 windowed exe 没 console 时弹 GUI）
    if args and args[0] in ("--help", "-h", "-?", "/?", "help"):
        print(_USAGE)
        return 0

    # 规则 0：剥 leading Python 全局 flag（console.py 第 1585 行用了 `-u -m todoseine`）
    while args:
        a = args[0]
        if a == "-m" or a.startswith("--") or a in _KNOWN_SUBCOMMANDS:
            break
        if a.startswith("-") and len(a) == 2 and a[1].isalpha():
            # 单字符 Python flag：-u -s -O -B -E -I 等
            args.pop(0)
        else:
            break

    # 规则 1：剥 "-m todoseine" 前缀 → 路由 CLI
    if len(args) >= 2 and args[0] == "-m" and args[1] == "todoseine":
        return _route_cli(args[2:])

    # 规则 2：args[0] 是 .py 文件路径 → snap_todo 等独立脚本
    if args and args[0].lower().endswith(".py"):
        if getattr(sys, "frozen", False):
            return _run_script_inprocess(args)
        return _route_script(args)

    # 规则 3：第一个参数是已知子命令 → CLI
    if args and args[0] in _KNOWN_SUBCOMMANDS:
        return _route_cli(args)

    # 规则 4：控制台（默认）
    return _route_console(args)


def _route_cli(args: list[str]) -> int:
    from todoseine.cli import main as cli_main

    try:
        return cli_main(args)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0


def _route_script(args: list[str]) -> int:
    """开发态：exec 一个独立 .py 脚本（snap_todo 等）。"""
    script = Path(args[0])
    if not script.exists():
        print(f"[todoseine] 脚本不存在：{script}", file=sys.stderr)
        return 1
    import subprocess

    proc = subprocess.run([sys.executable, str(script), *args[1:]])
    return proc.returncode


def _run_script_inprocess(args: list[str]) -> int:
    """frozen：用打包内的解释器直接执行随包的 .py（tools/ 已在 spec datas 里）。

    PyInstaller 的 windowed exe 无法再启动外部 Python 进程跑任意脚本，
    但可以用 runpy 在本进程内执行解包目录下的 .py，依赖标准库与已打包模块。
    """
    import runpy

    script = Path(args[0])
    if not script.exists():
        print(f"[todoseine] 脚本不存在：{script}", file=sys.stderr)
        return 1
    sys.argv = [str(script), *args[1:]]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0
    return 0


def _route_console(args: list[str]) -> int:
    try:
        from console import main as console_main  # noqa: PLC0415 — 延迟 import
        return console_main(args)
    except ImportError as exc:
        print(f"无法导入控制台：{exc}", file=sys.stderr)
        print("请确认 todoseine 已安装：pip install -e .", file=sys.stderr)
        return 1
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
