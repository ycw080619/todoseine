"""命令行入口：c2t。"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .adapters import REGISTRY, get_adapter
from .core import Store, dedupe, extract_todos, render_markdown


def _guess_adapter(path: str) -> str:
    lower = path.lower()
    if lower.endswith((".json", ".jsonl")):
        return "chatlab_json"
    return "qq_txt"


def cmd_import(args) -> int:
    adapter_name = args.adapter if args.adapter != "auto" else _guess_adapter(args.file)
    adapter = get_adapter(adapter_name)

    session = args.session or os.path.splitext(os.path.basename(args.file))[0]
    raw = list(adapter.read(args.file))
    unique = dedupe(raw)
    todos = extract_todos(unique, session=session)

    store = Store(args.db)
    try:
        added = store.add_messages(unique, session=session)
        added_todos = store.add_todos(todos)
        stats = store.stats()
    finally:
        store.close()

    print(f"适配器      : {adapter_name}")
    print(f"读到消息    : {len(raw)}")
    print(f"去重后      : {len(unique)}（新增入库 {added}）")
    print(f"抽取待办    : {len(todos)}（新增 {added_todos}）")
    print(f"库内总计    : 消息 {stats['messages']} / 待办 {stats['todos']} / 待处理 {stats['pending']}")

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        md = render_markdown(todos, title=f"待办清单 · {session}")
        path = os.path.join(args.out, "todos.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"Markdown    : {path}")
    return 0


def cmd_todos(args) -> int:
    store = Store(args.db)
    try:
        todos = store.list_todos(args.status)
    finally:
        store.close()
    if not todos:
        print("没有待办。")
        return 0
    for item in todos:
        mark = {"pending": " ", "unconfirmed": "?", "done": "x"}.get(item.status, " ")
        print(f"[{mark}] #{item.todo_id:<4} {item.text}")
        print(f"        {item.sender} · {item.reason} · {item.confidence:.2f}")
    return 0


def cmd_export_md(args) -> int:
    store = Store(args.db)
    try:
        todos = store.list_todos(args.status)
    finally:
        store.close()
    md = render_markdown(todos)
    if args.out:
        parent = os.path.dirname(os.path.abspath(args.out))
        os.makedirs(parent, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"已写入 {args.out}")
    else:
        sys.stdout.write(md)
    return 0


def cmd_stats(args) -> int:
    store = Store(args.db)
    try:
        for key, value in store.stats().items():
            print(f"{key:>10}: {value}")
    finally:
        store.close()
    return 0


def cmd_done(args) -> int:
    store = Store(args.db)
    try:
        store.conn.execute("UPDATE todos SET status='done' WHERE id=?", (args.id,))
        store.conn.commit()
    finally:
        store.close()
    print(f"#{args.id} 已标记完成")
    return 0


def cmd_adapters(_args) -> int:
    for name, cls in sorted(REGISTRY.items()):
        exts = ", ".join(cls.extensions) or "-"
        print(f"{name:<14} {exts}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="c2t",
        description="Chat2Todo —— 本地优先的聊天记录待办提取器",
    )
    parser.add_argument("--version", action="version", version=f"chat2todo {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="导入一份聊天记录导出文件")
    p.add_argument("file")
    p.add_argument("--db", default="c2t.db")
    p.add_argument("--adapter", default="auto", choices=["auto", *sorted(REGISTRY)])
    p.add_argument("--session", default="")
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("todos", help="列出待办")
    p.add_argument("--db", default="c2t.db")
    p.add_argument("--status", default=None, choices=["pending", "unconfirmed", "done"])
    p.set_defaults(func=cmd_todos)

    p = sub.add_parser("export-md", help="导出 Markdown")
    p.add_argument("--db", default="c2t.db")
    p.add_argument("--out", default="")
    p.add_argument("--status", default=None, choices=["pending", "unconfirmed", "done"])
    p.set_defaults(func=cmd_export_md)

    p = sub.add_parser("stats", help="查看统计")
    p.add_argument("--db", default="c2t.db")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("done", help="标记待办完成")
    p.add_argument("id", type=int)
    p.add_argument("--db", default="c2t.db")
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("adapters", help="列出可用适配器")
    p.set_defaults(func=cmd_adapters)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
