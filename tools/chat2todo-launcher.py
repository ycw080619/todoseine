"""Chat2Todo 统一入口：开发和打包都用这一个脚本。

路由规则（argv 处理顺序）：
  1. argv[:2] == ["-m", "chat2todo"]     → 剥掉前缀 → chat2todo.cli.main(argv[2:])
  2. argv[0] 是 chat2todo 子命令          → chat2todo.cli.main(argv)
  3. argv[0] == "--page" 或 以 "--page=" 开头 → 控制台（指定页面）
  4. 无参数 / 其他 → 控制台

开发时：直接 `python chat2todo-launcher.py` 或 `python chat2todo-launcher.py dash --db ...`
打包后：chat2todo.exe 本身就是这个入口。console.py 里的 subprocess 调用
       `[sys.executable, "-m", "chat2todo", "import", ...]` 会被这条规则接住。
"""

from __future__ import annotations

import sys
from pathlib import Path

# --- 路径自举：让 launcher 能 import chat2todo 包和 console 模块 ---
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
# console 模块的路径（tools/qq_db_export/）
CONSOLE_DIR = HERE / "qq_db_export"
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

# chat2todo 已知子命令（cli.py build_parser 里注册的全部）
_KNOWN_SUBCOMMANDS = {
    "import", "todos", "done", "search", "show", "export", "export-md",
    "stats", "doctor", "adapters", "dash",
    "db", "db-key", "db-import", "db-backfill",
    "purge-chat", "purge-unmarked", "watch",
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # 规则 1：console.py 用 subprocess 调 `[sys.executable, "-m", "chat2todo", ...]`
    # 打包后 sys.executable 就是这个 exe，所以前缀 "-m chat2todo" 需要剥掉
    if len(args) >= 2 and args[0] == "-m" and args[1] == "chat2todo":
        args = args[2:]
        return _route_cli(args)

    # 规则 2：第一个参数是已知子命令 → CLI 入口
    if args and args[0] in _KNOWN_SUBCOMMANDS:
        return _route_cli(args)

    # 规则 3：控制台（默认）
    return _route_console(args)


def _route_cli(args: list[str]) -> int:
    from chat2todo.cli import main as cli_main

    try:
        return cli_main(args)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0


def _route_console(args: list[str]) -> int:
    try:
        from console import main as console_main  # noqa: PLC0415 — 延迟 import，避免 tkinter 在纯 CLI 模式下被拉起来
        return console_main(args)
    except ImportError as exc:
        print(f"无法导入控制台：{exc}", file=sys.stderr)
        print("请确认 chat2todo 已安装：pip install -e .", file=sys.stderr)
        return 1
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
