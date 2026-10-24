"""命令行入口：c2t。

命令与文档第九章一一对应：
  import / todos / done / search / show / export / stats / doctor / adapters
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta

from . import __version__
from .adapters import REGISTRY, get_adapter
from .ai import AI
from .config import ConfigError, load, sample_toml
from .core import Store, dedupe, extract_todos, render_markdown, to_csv, to_json
from .errors import C2TError
from .models import TodoItem


# --- 辅助 ---------------------------------------------------------
def _guess_adapter(path: str) -> str:
    lower = path.lower()
    if lower.endswith((".json", ".jsonl")):
        return "chatlab_json"
    return "qq_txt"


def _cfg(args):
    cfg = load(getattr(args, "config", None))
    if getattr(args, "db", None):
        cfg["db_path"] = args.db
    return cfg


def _db(args) -> str:
    return _cfg(args)["db_path"]


def _fmt_ts(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(ts)


def _parse_since(text: str) -> int:
    """支持 7d / 24h / 30m；也接受纯数字（视为小时）。"""
    text = (text or "").strip().lower()
    if not text:
        return 0
    unit = text[-1]
    try:
        amount = int(text[:-1]) if unit in "dhm" else int(text)
    except ValueError:
        raise C2TError("E9001", f"--since 无法识别：{text}（例：7d / 24h / 30m）")
    delta = {
        "d": timedelta(days=amount),
        "h": timedelta(hours=amount),
        "m": timedelta(minutes=amount),
    }.get(unit, timedelta(hours=amount))
    return int((datetime.now() - delta).timestamp())


def _decode_to_temp(path: str, encoding: str) -> str:
    """--encoding 时把文件解码成临时 UTF-8，交给适配器读。"""
    try:
        with open(path, "r", encoding=encoding, errors="strict") as fh:
            text = fh.read()
    except (UnicodeDecodeError, LookupError) as exc:
        raise C2TError("E1001", f"{path} 用 {encoding} 解码失败：{exc}")
    fd, tmp = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    return tmp


# --- 命令 ---------------------------------------------------------
def cmd_import(args) -> int:
    if not os.path.exists(args.file):
        raise C2TError("E1001", f"找不到文件 {args.file}")

    src = args.file
    if args.encoding:
        src = _decode_to_temp(args.file, args.encoding)

    adapter_name = args.adapter if args.adapter != "auto" else _guess_adapter(args.file)
    if adapter_name not in REGISTRY:
        raise C2TError("E1002", f"没有名为 {adapter_name} 的适配器")
    adapter = get_adapter(adapter_name)

    cfg = _cfg(args)
    session = args.session or os.path.splitext(os.path.basename(args.file))[0]
    raw = list(adapter.read(src))
    unique = dedupe(raw, strict=bool(cfg.get("strict_dedupe")))
    todos = extract_todos(unique, session=session)

    # AI 默认全关；开启后失败也只是退回规则结果
    ai = AI(cfg)
    ai_note = ""
    if ai.calls == 0 and ai.enabled and not ai.usable:
        ai_note = "（AI 已启用但缺少 base_url/api_key，本次未调用）"
    if ai.on("review"):
        kept = []
        for item in todos:
            verdict = ai.review(item.text)
            if verdict is None or verdict:
                kept.append(item)
        ai_note = f"（AI 复核：{len(todos)} → {len(kept)}）"
        todos = kept
    if ai.on("rewrite"):
        for item in todos:
            better = ai.rewrite(item.text)
            if better:
                item.text = better
    if ai.on("intent"):
        kept = []
        for item in todos:
            verdict = ai.intent(item.text)
            if verdict is not False:          # None = 判断失败，保留规则结论
                kept.append(item)
        if len(kept) != len(todos):
            ai_note = f"{ai_note}（意图过滤：{len(todos)} → {len(kept)}）".strip()
        todos = kept
    if ai.on("merge") and len(todos) >= 2:
        merged = ai.merge([t.text for t in todos])
        if merged and len(merged) < len(todos):
            by_text = {t.text: t for t in todos}
            rebuilt = []
            for text in merged:
                base = by_text.get(text)
                if base is not None:
                    rebuilt.append(base)
                else:                          # AI 合并出的新描述：沿用首条元数据并降置信度
                    first = todos[0]
                    rebuilt.append(TodoItem(
                        text=text, session=first.session, sender=first.sender,
                        message_ts=first.message_ts, reason="AI 归并", confidence=0.70,
                    ))
            ai_note = f"{ai_note}（AI 归并：{len(todos)} → {len(rebuilt)}）".strip()
            todos = rebuilt

    try:
        store = Store(args.db or cfg["db_path"])
    except Exception as exc:  # noqa: BLE001
        raise C2TError("E2001", str(exc))
    try:
        added = store.add_messages(unique, session=session)
        added_todos = store.add_todos(todos)
        stats = store.stats()
    finally:
        store.close()

    print(f"适配器      : {adapter_name}")
    print(f"读到消息    : {len(raw)}")
    print(f"去重后      : {len(unique)}（新增入库 {added}）")
    print(f"抽取待办    : {len(todos)}（新增 {added_todos}）{ai_note}")
    print(f"库内总计    : 消息 {stats['messages']} / 待办 {stats['todos']} / 待处理 {stats['pending']}")

    if args.out:
        _write_export(args.out, "todos.md", render_markdown(todos, title=f"待办清单 · {session}"))
    return 0


def _write_export(directory: str, name: str, content: str) -> str:
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        raise C2TError("E3001", f"{directory}：{exc}")
    print(f"已写入      : {path}")
    return path


def cmd_todos(args) -> int:
    cfg = _cfg(args)
    db = cfg["db_path"]
    store = Store(db)
    try:
        todos = store.list_todos(args.status)
    finally:
        store.close()
    if not todos:
        print("没有待办。")
        return 0
    for idx, item in enumerate(todos, 1):
        mark = {"pending": " ", "unconfirmed": "?", "done": "x"}.get(item.status, " ")
        where = f"{item.session} · {item.sender}" if item.session else item.sender
        print(f"{idx:>2}. [{mark}] {item.text:<28} {where:<12} {item.confidence:.2f}")
        if args.verbose:
            print(f"       #{item.todo_id} 依据：{item.reason}")
    return 0


def cmd_done(args) -> int:
    store = Store(_db(args))
    try:
        ok = store.set_status(args.id, "done")
    finally:
        store.close()
    if not ok:
        print(f"没有编号为 {args.id} 的待办。", file=sys.stderr)
        return 1
    print(f"#{args.id} 已标记完成")
    return 0


def cmd_search(args) -> int:
    store = Store(_db(args))
    try:
        hits = store.search(args.keyword, limit=args.limit)
    finally:
        store.close()
    if not hits:
        print(f"没有找到包含「{args.keyword}」的消息。")
        return 0
    for row in hits:
        print(f"#{row['id']:<5} {_fmt_ts(row['ts'])} {row['sender_name']}: {row['content']}")
    print(f"\n共 {len(hits)} 条（用 c2t show <编号> 看上下文）")
    return 0


def cmd_show(args) -> int:
    store = Store(_db(args))
    try:
        rows = store.context(args.msg_id, before=args.before, after=args.after)
    finally:
        store.close()
    if not rows:
        print(f"没有编号为 {args.msg_id} 的消息。")
        return 1
    for row in rows:
        mark = ">" if row["id"] == args.msg_id else " "
        print(f"{mark}#{row['id']:<5} {_fmt_ts(row['ts'])} {row['sender_name']}: {row['content']}")
    return 0


def cmd_export(args) -> int:
    cfg = _cfg(args)
    store = Store(cfg["db_path"])
    try:
        todos = store.list_todos(args.status)
    finally:
        store.close()

    if args.format == "csv":
        payload = to_csv(todos)
    elif args.format == "json":
        payload = to_json(todos, meta={"session": args.session, "count": len(todos)})
    elif cfg.get("obsidian_vault"):
        payload = _obsidian_md(todos, cfg["obsidian_vault"])
    else:
        payload = render_markdown(todos)

    if args.out:
        _write_export(args.out, f"todos.{args.format}", payload)
    else:
        sys.stdout.write(payload)
    return 0


def _obsidian_md(todos, vault: str) -> str:
    lines = [f"# 待办清单（Obsidian 双链）", ""]
    for item in todos:
        box = "x" if item.status == "done" else " "
        src = f"[[{item.session}]]" if item.session else "[[未命名]]"
        lines.append(f"- [{box}] {item.text} — {src} · {item.sender}")
    return "\n".join(lines) + "\n"


def cmd_stats(args) -> int:
    since = _parse_since(args.since) if getattr(args, "since", "") else None
    store = Store(_db(args))
    try:
        data = store.stats(since_ts=since)
    finally:
        store.close()
    scope = f"（{args.since} 内）" if getattr(args, "since", "") else "（全部）"
    print(f"统计{scope}")
    for key, value in data.items():
        print(f"  {key:>10}: {value}")
    return 0


def cmd_doctor(args) -> int:
    cfg = _cfg(args)
    problems = 0

    def line(ok: bool, label: str, detail: str = "") -> None:
        nonlocal problems
        mark = "OK  " if ok else "FAIL"
        if not ok:
            problems += 1
        print(f"[{mark}] {label}" + (f"  {detail}" if detail else ""))

    line(sys.version_info >= (3, 9), "Python 版本", sys.version.split()[0])

    try:
        import sqlite3
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            fts = True
        except sqlite3.OperationalError:
            fts = False
        conn.close()
    except Exception:  # noqa: BLE001
        fts = False
    line(fts, "SQLite FTS5 全文索引", "缺失时检索自动退回模糊匹配")
    line(True, "SQLite 版本", getattr(__import__("sqlite3"), "sqlite_version", "?"))

    conf = cfg.get("config_file") or ""
    line(True, "配置文件", conf or "未找到，使用默认值")
    line(len(REGISTRY) > 0, "适配器", f"{len(REGISTRY)} 个：{', '.join(sorted(REGISTRY))}")

    db_dir = os.path.dirname(os.path.abspath(cfg["db_path"])) or "."
    line(os.access(db_dir, os.W_OK), "数据库目录可写", db_dir)

    export_dir = args.export_dir or cfg["export_dir"]
    try:
        os.makedirs(export_dir, exist_ok=True)
        probe = os.path.join(export_dir, ".c2t_write_test")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        line(True, "导出目录可写", export_dir)
    except OSError:
        line(False, "导出目录可写", export_dir)

    ai = cfg.get("ai") or {}
    if ai.get("enabled"):
        line(bool(ai.get("base_url")) and bool(ai.get("api_key") or os.environ.get("C2T_AI_KEY")),
             "AI 配置", "已启用；缺 base_url 或 api_key" if not ai.get("base_url") else "已启用")
    else:
        line(True, "AI 配置", "未启用（纯规则模式，功能完整）")

    if getattr(args, "write_sample", False):
        target = args.config or "c2t.toml"
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(sample_toml())
        print(f"\n已生成配置示例：{target}")

    print(f"\n结论：{'发现 ' + str(problems) + ' 个问题' if problems else '环境正常'}")
    return 0 if problems == 0 else 1


def cmd_adapters(_args) -> int:
    for name, cls in sorted(REGISTRY.items()):
        exts = ", ".join(getattr(cls, "extensions", []) or ["-"]) or "-"
        print(f"{name:<14} {exts}")
    return 0


def cmd_dash(args) -> int:
    """置顶小窗看板；纯界面，不影响数据与命令行。"""
    from .dashboard import run

    status = args.status or "pending"
    return run(_db(args), interval=int(args.interval), status=status)


def cmd_db(args) -> int:
    """检查数据库只读通道：库在哪里、是不是加密、有哪些后端可用。"""
    from .sources import BuiltinBackend, CommunityBackend, inspect, locate

    cfg = _cfg(args)
    db_cfg = cfg.get("db", {})
    path = locate(db_cfg.get("path") or None)
    if not path:
        print("[E4001] 未找到 nt_msg.db")
        print("  建议：用 [db].path 指定，或改用导出文件导入")
        return 1
    info = inspect(str(path))
    print(f"数据库    : {info.path}")
    print(f"大小      : {info.size / 1048576:.1f} MB（{info.pages} 页 × 4096）")
    print(f"加密      : {'是（腾讯 WCDB 变体）' if info.encrypted else '否，可直接用 sqlite 打开'}")
    community = CommunityBackend(module=db_cfg.get("module", ""))
    builtin = BuiltinBackend(key=db_cfg.get("key", ""), key_file=db_cfg.get("key_file", ""))
    print(f"社区后端  : {'可用 → ' + db_cfg.get('module', '') if community.available() else '未配置'}")
    print(f"内置后端  : {'有 key，可尝试' if builtin.available() else '未提供 key'}")
    if builtin.available():
        from .core.crypto import verify_sqlcipher_key

        ok = verify_sqlcipher_key(builtin._key(), info.path)
        print(f"密钥校验  : {'通过，可解开首页（SQLite format 3）' if ok else '未通过——密钥或 KDF 参数不对'}")
    if not community.available() and not builtin.available():
        print("  → 现在无法读取；配置 [db].module 或 [db].key 后重试")
    return 0


def cmd_db_import(args) -> int:
    """从 QQ 数据库只读拉取消息，走与文件导入相同的抽取管线。"""
    from .core import dedupe, extract_todos
    from .sources import load_messages

    cfg = _cfg(args)
    messages = load_messages(cfg, limit=int(args.limit))
    if not messages:
        print("没有读到消息（可能后端返回空）")
        return 1
    messages = dedupe(messages)
    todos = extract_todos(messages, session=args.session or "qq")
    store = Store(_db(args))
    try:
        added = store.add_messages(messages)
        tadded = store.add_todos(todos)
        s = store.stats()
    finally:
        store.close()
    print(f"来源        : 数据库（只读）")
    print(f"读到消息    : {len(messages)}")
    print(f"去重后      : {len(messages)}（新增入库 {added}）")
    print(f"抽取待办    : {len(todos)}（新增 {tadded}）")
    print(f"库内总计    : 消息 {s['messages']} / 待办 {s['todos']} / 待处理 {s['pending']}")
    return 0


# --- 参数 ---------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="c2t", description="Chat2Todo —— 本地优先的聊天记录待办提取器",
    )
    parser.add_argument("--version", action="version", version=f"chat2todo {__version__}")
    parser.add_argument("--config", default="", help="指定配置文件")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="导入一份聊天记录导出文件")
    p.add_argument("file")
    p.add_argument("--db", default="", help="默认取配置 db_path")
    p.add_argument("--adapter", default="auto", choices=["auto", *sorted(REGISTRY)])
    p.add_argument("--session", default="")
    p.add_argument("--out", default="")
    p.add_argument("--encoding", default="", help="文件编码，例：gbk")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("todos", help="列出待办")
    p.add_argument("--db", default="")
    p.add_argument("--status", default=None, choices=["pending", "unconfirmed", "done"])
    p.add_argument("-v", "--verbose", action="store_true", help="同时显示判定依据")
    p.set_defaults(func=cmd_todos)

    p = sub.add_parser("done", help="标记待办完成")
    p.add_argument("id", type=int)
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("search", help="检索原文")
    p.add_argument("keyword")
    p.add_argument("--db", default="")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", help="查看原文上下文")
    p.add_argument("msg_id", type=int)
    p.add_argument("--db", default="")
    p.add_argument("--before", type=int, default=2)
    p.add_argument("--after", type=int, default=2)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("export", help="导出待办（md/csv/json）")
    p.add_argument("--format", default="md", choices=["md", "csv", "json"])
    p.add_argument("--db", default="")
    p.add_argument("--out", default="")
    p.add_argument("--status", default=None, choices=["pending", "unconfirmed", "done"])
    p.add_argument("--session", default="")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("stats", help="统计")
    p.add_argument("--db", default="")
    p.add_argument("--since", default="", help="时间窗：7d / 24h / 30m")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("doctor", help="环境自检")
    p.add_argument("--db", default="")
    p.add_argument("--export-dir", default="")
    p.add_argument("--write-sample", action="store_true", help="生成一份配置示例")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("adapters", help="列出适配器")
    p.set_defaults(func=cmd_adapters)

    p = sub.add_parser("dash", help="打开置顶待办看板（需要图形环境）")
    p.add_argument("--db", default="")
    p.add_argument("--interval", type=int, default=30, help="自动刷新秒数")
    p.add_argument("--status", default="pending", choices=["pending", "unconfirmed", "done"])
    p.set_defaults(func=cmd_dash)

    p = sub.add_parser("db", help="检查数据库只读通道（需要解密后端）")
    p.set_defaults(func=cmd_db)

    p = sub.add_parser("db-import", help="从 QQ 数据库只读拉取消息并抽取待办")
    p.add_argument("--db", default="")
    p.add_argument("--limit", type=int, default=0, help="只取最近 N 条，0 表示全部")
    p.add_argument("--session", default="")
    p.set_defaults(func=cmd_db_import)

    # 兼容旧命令名
    p = sub.add_parser("export-md", help="导出 Markdown（等同于 export --format md）")
    p.add_argument("--db", default="")
    p.add_argument("--out", default="")
    p.add_argument("--status", default=None, choices=["pending", "unconfirmed", "done"])
    p.set_defaults(func=cmd_export, format="md", session="")

    # 让 --config 写在子命令后面也能用（argparse 默认只认全局位置）
    for _p in sub.choices.values():
        _p.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "config"):
        args.config = ""
    try:
        return args.func(args)
    except C2TError as err:
        print(err.render(), file=sys.stderr)
        return 1
    except ConfigError as err:
        print(f"[E0001] 配置文件无法解析\n  细节：{err}\n  建议：检查 TOML 语法", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
