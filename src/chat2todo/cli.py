"""命令行入口：c2t。

命令与文档第九章一一对应：
  import / todos / done / search / show / export / stats / doctor / adapters
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

from . import __version__
from .adapters import REGISTRY, get_adapter
from .ai import AI
from .config import ConfigError, load, sample_toml
from .core import Store, dedupe, extract_todos, merge_events, render_markdown, to_csv, to_json
from .core.privacy import install_log_filter
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


def _disp_width(text: str) -> int:
    """显示宽度：全角字符按 2 算，否则中文群名会把表格顶歪。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    """按显示宽度右补空格到 width；超长不截断，保证可读。"""
    return text + " " * max(0, width - _disp_width(text))


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
    """--encoding 时把文件解码成临时 UTF-8，交给适配器读。

    返回值是临时文件路径，调用方用完必须删掉——这里是完整的明文聊天记录，
    留在 %TEMP% 里等于每次导入都多一份副本。
    """
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


def _drop_temp(path: str) -> None:
    """删掉 _decode_to_temp 落下的明文临时文件（失败不报错）。"""
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


# --- 命令 ---------------------------------------------------------
def cmd_import(args) -> int:
    if not os.path.exists(args.file):
        raise C2TError("E1001", f"找不到文件 {args.file}")

    src = args.file
    tmp_src = ""
    if args.encoding:
        src = tmp_src = _decode_to_temp(args.file, args.encoding)

    adapter_name = args.adapter if args.adapter != "auto" else _guess_adapter(args.file)
    if adapter_name not in REGISTRY:
        raise C2TError("E1002", f"没有名为 {adapter_name} 的适配器")
    adapter = get_adapter(adapter_name)

    cfg = _cfg(args)
    session = args.session or os.path.splitext(os.path.basename(args.file))[0]
    try:
        raw = list(adapter.read(src))
    finally:
        _drop_temp(tmp_src)
    unique = dedupe(raw, strict=bool(cfg.get("strict_dedupe")))

    todos, decisions, llm_records, notes = _run_extraction(unique, cfg, session)

    try:
        store = Store(args.db or cfg["db_path"])
    except Exception as exc:  # noqa: BLE001
        raise C2TError("E2001", str(exc))
    try:
        added = store.add_messages(unique, session=session)
        added_todos = store.add_todos(todos)
        store.add_decisions(decisions)
        store.add_llm_calls(llm_records)
        stats = store.stats()
    finally:
        store.close()

    print(f"适配器      : {adapter_name}")
    print(f"读到消息    : {len(raw)}")
    print(f"去重后      : {len(unique)}（新增入库 {added}）")
    print(f"抽取待办    : {len(todos)}（新增 {added_todos}）{notes}")
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


def _run_extraction(messages, cfg: dict, session: str,
                    weights_map: dict = None) -> tuple:
    """统一抽取管线：extract → 跨群合并 → 语义清洗 → AI。

    返回 (todos, decisions, llm_records, notes)。
    不触碰数据库，落库由调用方负责。
    """
    from .semantic import vector_pass

    decisions: list = []
    todos = extract_todos(messages, session=session,
                          weights_map=weights_map,
                          todo_threshold=cfg.get("todo_threshold"),
                          weak_threshold=cfg.get("pending_threshold"),
                          decisions=decisions)

    todos, ev_note = merge_events(todos, messages)
    todos, vec_note = vector_pass(todos, cfg)

    llm_records: list = []
    ai = AI(cfg, on_call=llm_records.append)
    ai_note = ""
    if ai.calls == 0 and ai.enabled and not ai.usable:
        if ai.base_url_rejected:
            ai_note = ("（AI 已启用但 base_url 被安全检查拒绝："
                       "明文 http 只允许本机/内网地址，本次未调用）")
        else:
            ai_note = "（AI 已启用但缺少 base_url/api_key，本次未调用）"
    if ai.on("review"):
        kept = [t for t in todos if ai.review(t.text, t.session) is not False]
        ai_note = f"{ai_note}（AI 复核 {len(todos)}→{len(kept)}）".strip()
        todos = kept
    if ai.on("intent"):
        kept = [t for t in todos if ai.intent(t.text, t.session) is not False]
        ai_note = f"{ai_note}（意图过滤 {len(todos)}→{len(kept)}）".strip()
        todos = kept
    if ai.on("rewrite"):
        for t in todos:
            better = ai.rewrite(t.text, t.session)
            if better:
                t.text = better
        ai_note = f"{ai_note}（AI 改写）".strip()
    if ai.on("merge") and len(todos) >= 2:
        merged = ai.merge([t.text for t in todos], todos[0].session)
        if merged and len(merged) < len(todos):
            by_text = {t.text: t for t in todos}
            rebuilt = []
            for text in merged:
                base = by_text.get(text)
                if base is not None:
                    rebuilt.append(base)
                else:
                    first = todos[0]
                    rebuilt.append(TodoItem(
                        text=text, session=first.session, sender=first.sender,
                        message_ts=first.message_ts, reason="AI 归并", confidence=0.70,
                    ))
            ai_note = f"{ai_note}（AI 归并 {len(todos)}→{len(rebuilt)}）".strip()
            todos = rebuilt

    # AI 一次都没成功时不能报「AI 复核过」：那会让用户以为有人工智能把关，
    # 实际跑的是纯规则结果（密钥过期、断网、端点 500 都会走到这里）。
    if llm_records and all(r.get("failed") for r in llm_records):
        ai_note = f"{ai_note}（AI 调用全部失败，已退回纯规则结果）".strip()

    notes = ev_note + vec_note + ai_note
    return todos, decisions, llm_records, notes


def _pad(text: str, width: int) -> str:
    """按终端显示宽度补空格：中文是全角，按字符数补会让列错位。"""
    flat = " ".join((text or "").split())
    shown = sum(2 if ord(ch) > 127 else 1 for ch in flat)
    if shown <= width:
        return flat + " " * (width - shown)
    out, acc = "", 0
    for ch in flat:
        w = 2 if ord(ch) > 127 else 1
        if acc + w > width - 2:
            break
        out += ch
        acc += w
    return out + "…" + " " * max(0, width - acc - 2)


def cmd_todos(args) -> int:
    cfg = _cfg(args)
    db = cfg["db_path"]
    store = Store(db)
    try:
        todos = store.list_todos(args.status)
    finally:
        store.close()
    if not args.status:
        # 被用户否掉的 noise / 自己静音掉的 muted 都不该混在待办列表里：
        # 混进去后「哪些是我处理过的」就完全看不出来了
        todos = [t for t in todos if t.status not in ("noise", "muted")]
    if not todos:
        print("没有待办。")
        return 0
    for idx, item in enumerate(todos, 1):
        mark = {"pending": " ", "unconfirmed": "?", "done": "x"}.get(item.status, "-")
        where = f"{item.session} · {item.sender}" if item.session else item.sender
        print(f"{idx:>4}. [{mark}] {_pad(item.text, 46)} {_pad(where, 18)} "
              f"{item.confidence:.2f}")
        if args.verbose:
            print(f"        #{item.todo_id} 依据：{item.reason}")
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


def cmd_mute(args) -> int:
    """对某条待办设置「此类消息以后不再提醒」。"""
    store = Store(_db(args))
    try:
        todo = next((t for t in store.list_todos() if t.todo_id == args.id), None)
        if todo is None:
            print(f"没有编号为 {args.id} 的待办。", file=sys.stderr)
            return 1
        if args.scope == "source":
            res = store.mute(todo.session, "")
            scope = "整个来源"
        elif args.scope == "sender":
            res = store.mute(todo.session, todo.sender)
            scope = f"{todo.sender}"
        else:
            res = store.mute(todo.session, "", todo.text)
            scope = f"此类消息（样板：{todo.text[:24]}…）"
    finally:
        store.close()
    print(f"已静音（{scope}）：规则 #{res['rule_id']}，"
          f"同时收起 {res['muted_todos']} 条已有待办")
    print("以后相似的消息不再生成提醒；撤销用 c2t unmute <规则编号>")
    return 0


def cmd_unmute(args) -> int:
    store = Store(_db(args))
    try:
        res = store.unmute(args.id)
    finally:
        store.close()
    if not res["found"]:
        print(f"没有编号为 {args.id} 的静音规则。", file=sys.stderr)
        return 1
    print(f"规则 #{args.id} 已撤销，恢复 {res['restored']} 条待办")
    return 0


def cmd_mutes(args) -> int:
    store = Store(_db(args))
    try:
        rules = store.list_mutes()
    finally:
        store.close()
    if not rules:
        print("没有静音规则。")
        return 0
    for r in rules:
        if r["kind"] == "kind":
            where = f"此类：{r['text'][:20]}"
        elif r["kind"] == "sender":
            where = f"{r['session']} · {r['sender']}"
        else:
            where = f"{r['session'] or '全部来源'}（整群）"
        print(f"{r['id']:>4}. {_pad(where, 30)} 已收起 {r['muted_count']} 条  "
              f"{r['created_at']}")
    print(f"\n共 {len(rules)} 条（撤销用 c2t unmute <编号>）")
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


def cmd_explain(args) -> int:
    """打印某条待办的完整判定报告（规则 / 分数 / 用户校准命中 / 原消息上下文）。"""
    store = Store(_db(args))
    try:
        report = store.explain_todo(args.todo_id,
                                    threshold=_cfg(args).get("todo_threshold"))
    finally:
        store.close()
    if not report:
        print(f"没有编号为 {args.todo_id} 的待办。")
        return 1
    t = report["todo"]
    star = " ★重点" if report["star"] else ""
    print("╔═══════════════════════════════════════════════════")
    print(f"║ 待办 #{t['id']}{star}  状态: {t['status']}  置信度: {t['confidence']:.2f}")
    print(f"║ 发送者: {report['sender']}  会话: {t['session']}")
    if report["chat_id"]:
        print(f"║ 群号: {report['chat_id']}")
    print("╚═══════════════════════════════════════════════════")
    print(f"  内容: {t['text']}")
    print()

    # 原消息
    if report["original_msg"]:
        m = report["original_msg"]
        print("── 原消息 ──")
        print(f"  [{_fmt_ts(m['ts'])}] {m['sender_name']}: {m['content']}")
        print()

    # 规则分解
    print(f"── 规则判定分解（总分 {report['final_score']:.1f}，阈值 {report['threshold']}）──")
    if not report["tags"]:
        print("  （这条是手动添加或 reason 字段为空）")
    for item in report["tags"]:
        sign = "+" if item["base_score"] >= 0 else ""
        line = f"  {item['tag']:<22} {sign}{item['base_score']} → {item['description']}"
        if item["adj"] != 0.0:
            adj_sign = "+" if item["adj_score"] >= 0 else ""
            line += f"  [校准 {adj_sign}{item['adj_score']:.1f}]"
        print(line)
    print(f"  {'─' * 42}")
    subtotal_sign = "+" if report["subtotal"] >= 0 else ""
    print(f"  规则合计 {subtotal_sign}{report['subtotal']}"
          f"  校准 Δ{report['calib_delta']:+.1f}"
          f"  = {report['final_score']:.1f}")
    print()

    # 校准权重
    if report["weights_hit"]:
        print("── 本组用户校准权重 ──")
        for k, v in report["weights_hit"].items():
            print(f"  {k}: {v}")
        print()

    # 附近消息上下文
    if report["nearby"]:
        print("── 上下文（原消息前后各 2 条）──")
        for row in report["nearby"]:
            mark = ">" if row["id"] == report["original_msg"]["id"] else " "
            print(f"  {mark}#{row['id']:<5} {_fmt_ts(row['ts'])} {row['sender_name']}: {row['content'][:60]}")
    return 0


def cmd_plugins(args) -> int:
    """扫描 tools/*/plugin.manifest.json，校验后打表。"""
    from pathlib import Path
    from .plugin_manifest import validate_manifest, ManifestError

    # 开发态：src/chat2todo → repo；frozen：--add-data 把 tools/ 塞进了
    #   sys._MEIPASS 临时解包目录（onefile 模式下），去那里找
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            tools_dir = Path(meipass) / "tools"
        else:
            tools_dir = Path(sys.executable).resolve().parent / "tools"
    else:
        src = Path(__file__).resolve().parent
        tools_dir = src.parent.parent / "tools"

    manifests = sorted(tools_dir.glob("*/plugin.manifest.json"))
    if not manifests:
        print("（未发现任何插件 manifest，把 plugin.manifest.json 放进 tools/<plugin_id>/）")
        return 0

    rows = []
    errors = []
    for path in manifests:
        rel_parent = path.parent.name
        try:
            m = validate_manifest(str(path))
            rows.append((m.id, m.name, m.version, m.permission_level,
                         ", ".join(m.capabilities) or "-", rel_parent))
        except ManifestError as exc:
            errors.append((rel_parent, str(exc)))

    if rows:
        print(f"{'ID':<16} {'名称':<14} {'版本':<8} {'权限':<6} {'能力':<24} 位置")
        print("─" * 72)
        for rid, name, ver, perm, caps, loc in rows:
            print(f"{rid:<16} {name:<14} {ver:<8} {perm:<6} {caps:<24} {loc}/")
    if errors:
        print()
        print("⚠ 有 manifest 校验失败：")
        for loc, msg in errors:
            print(f"  tools/{loc}: {msg}")
    return 0 if not errors else 1


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
    lines = ["# 待办清单（Obsidian 双链）", ""]
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


def cmd_dash_qt(args) -> int:
    """Qt 版看板（托盘 / 玻璃 / 实时监听）。需 .[qt] 依赖。"""
    try:
        from .qt.main import main as qt_main
    except ImportError as exc:
        print(f"Qt 版依赖缺失：{exc}")
        print("请先安装：pip install -e .[qt]")
        return 1
    return qt_main()


def cmd_db_key(args) -> int:
    """数据库口令管理：DPAPI 加密存本机，只有当前 Windows 用户能解开。"""
    from .core import keybox
    from .sources import locate

    cfg = _cfg(args)
    db_cfg = cfg.get("db", {})

    if args.delete:
        print("已删除本机保存的口令" if keybox.delete_key() else "本机没有保存过口令")
        return 0

    if args.save:
        import getpass

        key = getpass.getpass("请输入 QQ 数据库口令（输入时不显示）：").strip()
        if len(key) < 8:
            print("口令太短，已放弃")
            return 1
        # 能找到库就先验证一把，解不开首页的口令存了也没用
        path = locate(db_cfg.get("path") or None)
        if path:
            from .core.crypto import verify_sqlcipher_key

            ok = verify_sqlcipher_key(key, str(path), iterations=4000, digest="sha512")
            if not ok and not args.force:
                print("[E4003] 该口令解不开当前数据库首页，未保存")
                print("  建议：保持 QQ 登录运行，用提取工具重新扫描；执意保存请加 --force")
                return 1
        target = keybox.save_key(key)
        print(f"已加密保存到 {target}（DPAPI，仅当前用户可解）")
        return 0

    if args.check:
        saved = keybox.load_key()
        print(f"本机密库  : {'有口令（' + keybox.key_file_path().__str__() + '）' if saved else '空'}")
        env = bool(os.environ.get("C2T_DB_KEY"))
        print(f"环境变量  : {'已设置 C2T_DB_KEY' if env else '未设置'}")
        conf = "已配置" if db_cfg.get("key") or db_cfg.get("key_file") else "未配置"
        print(f"配置文件  : {conf}")
        return 0

    print("用法：c2t db-key --save | --check | --delete")
    return 1


def cmd_db(args) -> int:
    """检查数据库只读通道：库在哪里、是不是加密、引擎和密钥是否就绪。"""
    from .sources import BuiltinBackend, CommunityBackend, inspect, locate, resolve_key

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
    print(f"社区后端  : {'可用 → ' + db_cfg.get('module', '') if community.available() else '未配置'}")

    engine = BuiltinBackend.engine_available()
    print(f"内置引擎  : {'sqlcipher3 已就绪' if engine else '缺 sqlcipher3：pip install chat2todo[db]'}")

    key = resolve_key(db_cfg)
    if not key:
        print("密钥      : 未找到 → 运行 c2t db-key --save 交互保存（DPAPI 加密）")
    elif engine and info.encrypted:
        ok = BuiltinBackend(key=key)
        try:
            ok.fetch(info, limit=1)
            print("密钥      : 通过（试读 1 条成功）")
        except C2TError as exc:
            print(f"密钥      : 试读失败——{exc.detail}")
    else:
        print("密钥      : 已提供")
    if not community.available() and not (engine and key):
        print("  → 现在无法读取；先装引擎、存密钥后重试")
        return 1
    return 0


def cmd_db_import(args) -> int:
    """从 QQ 数据库只读拉取消息，走与文件导入相同的抽取管线。"""
    from .core import dedupe
    from .sources import load_messages

    cfg = _cfg(args)
    days = int(args.days or cfg.get("db", {}).get("days", 0) or 0)
    messages = load_messages(cfg, limit=int(args.limit), days=days)
    if not messages:
        print("没有读到消息（可能后端返回空）")
        return 1
    messages = dedupe(messages)

    # 用户校准权重：如果本地库已存在，先查 feedback 表聚合成权重字典
    weights = None
    try:
        store = Store(_db(args))
        try:
            weights = store.load_feedback_weights() or None
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 - 权重加载失败不阻塞导入
        logging.getLogger("chat2todo.cli").debug("校准权重加载失败，按默认权重打分：%s", exc)

    todos, decisions, llm_records, notes = _run_extraction(
        messages, cfg, args.session or "qq", weights_map=weights)

    store = Store(_db(args))
    try:
        added = store.add_messages(messages)
        tadded = store.add_todos(todos)
        store.add_decisions(decisions)
        store.add_llm_calls(llm_records)
        # 顺带刷新群名表（群名在同级另一个库里；失败静默，不影响同步）
        try:
            from .sources import fetch_group_names
            store.set_chat_names(fetch_group_names(cfg))
        except Exception:  # noqa: BLE001
            pass
        s = store.stats()
    finally:
        store.close()
    print("来源        : 数据库（只读）")
    print(f"读到消息    : {len(messages)}")
    print(f"去重后      : {len(messages)}（新增入库 {added}）")
    print(f"抽取待办    : {len(todos)}（新增 {tadded}）{notes}")
    print(f"库内总计    : 消息 {s['messages']} / 待办 {s['todos']} / 待处理 {s['pending']}")
    return 0


def cmd_db_backfill(args) -> int:
    """只给历史消息补来源群，不重新抽取待办（不调用 AI）。"""
    from .sources import load_messages

    cfg = _cfg(args)
    days = int(args.days or 0)
    messages = load_messages(cfg, days=days, keep_ignored=True)
    if not messages:
        print("没有读到消息")
        return 1
    store = Store(_db(args))
    try:
        filled = store.backfill_chat_ids(
            (m.timestamp, m.sender_id, len(m.content or ""), m.content_hash, m.chat_id)
            for m in messages
        )
        remain = store.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ''"
        ).fetchone()[0]
    finally:
        store.close()
    print(f"扫描消息    : {len(messages)}")
    print(f"回填来源群  : {filled} 条")
    print(f"仍未标记    : {remain} 条（导出文件导入的旧消息没有来源群，属正常）")
    return 0


def cmd_backup(args) -> int:
    """立即做一次库备份（默认 DPAPI 加密，保留最近 7 份）。"""
    from .core.backup import backup_database

    db = _db(args)
    path = backup_database(
        db,
        backup_dir=str(getattr(args, "dir", "") or ""),
        keep=int(getattr(args, "keep", 7) or 7),
        encrypt=not getattr(args, "no_encrypt", False),
    )
    if not path:
        print("备份失败：数据库不存在或写入出错")
        return 1
    print(f"备份完成：{path}")
    return 0


def cmd_wipe(args) -> int:
    """一键清空程序生成内容；必须显式 --yes，且不会碰插件本体与 Obsidian 其他笔记。"""
    from .core.maintenance import wipe_all

    if not getattr(args, "yes", False):
        print("一键清空会删除：数据库、Markdown 归档、备份、日志、缓存、插件配置。")
        print("插件本体与 Obsidian 库中的其他笔记不会被删除。")
        print("确认要执行请追加 --yes")
        return 1
    cfg = _cfg(args)
    removed = wipe_all(cfg)
    print(f"已清空 {len(removed)} 项：")
    for path in removed:
        print(f"  - {path}")
    return 0


def cmd_obsidian_sync(args) -> int:
    """双向同步：先回写 Obsidian 勾选状态，再把本地待办镜像过去。"""
    from .core.obsidian import make_sync

    cfg = _cfg(args)
    if not str(cfg.get("obsidian_vault") or ""):
        print("未配置 obsidian_vault，请先在设置里指定 Obsidian 库路径")
        return 1
    store = Store(cfg["db_path"])
    try:
        sync = make_sync(cfg, store)
        if sync is None:
            print(f"Obsidian 库不存在或不可写：{cfg.get('obsidian_vault')}")
            return 1
        result = sync.sync()
        print(f"完成回写 {result['done']} 条 · 镜像文件 {sync.note}")
        return 0
    finally:
        store.close()


def cmd_desktop_export(args) -> int:
    """一次性输出脱敏 JSON/ICS；--serve 时同时起本地回写接口直到 Ctrl+C。"""
    from .core.desktop_link import DesktopLink

    cfg = _cfg(args)
    cfg.setdefault("desktop", {})["enabled"] = True
    if args.dir:
        cfg["desktop"]["dir"] = args.dir
    link = DesktopLink(cfg, cfg["db_path"])
    if not link.export():
        print("输出失败：请检查数据库与输出目录")
        return 1
    print(f"待办文件    : {link.folder}")
    if not args.serve:
        return 0
    url = link.start_server()
    if not url:
        print("本地回写接口启动失败")
        return 1
    print(f"回写接口    : {url}")
    print(f"token 文件  : {os.path.join(link.folder, 'token.txt')}")
    print("按 Ctrl+C 结束")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        link.stop_server()
    return 0


def cmd_desktop_token(args) -> int:
    """查看 / 轮换桌面联动回写 token。"""
    from .core.desktop_link import ensure_token, out_dir

    cfg = _cfg(args)
    folder = out_dir(cfg, cfg["db_path"])
    token = ensure_token(folder, rotate=bool(args.rotate))
    if not token:
        print("token 生成失败：请检查输出目录权限")
        return 1
    print(token)
    if args.rotate:
        print("旧 token 已失效；重启程序后本地接口才会使用新 token。")
    return 0


def cmd_migrate(args) -> int:
    """数据库 schema 迁移：迁移前自动备份，失败自动放弃（正式库不动）。"""
    from .core.migrate import plan, run

    cfg = _cfg(args)
    db = cfg["db_path"]
    info = plan(db)
    print(f"数据库      : {db}")
    print(f"当前版本    : {info['current'] or '（空库/无 meta）'}")
    print(f"目标版本    : {info['target']}")
    if not info["exists"]:
        print("库文件还不存在；首次运行程序时会自动建库。")
        return 1
    if not info["needed"]:
        print("已是最新版本，无需迁移。")
        return 0
    if args.dry_run:
        print("（--dry-run：只展示，不做任何改动）")
        return 0
    result = run(db, str(cfg.get("backup", {}).get("dir") or ""))
    if not result["ok"]:
        print(f"迁移失败    : {result['error']}", file=sys.stderr)
        print("正式库未被修改，可直接继续使用。")
        return 1
    print(f"迁移完成    : {result['from']} → {result.get('after') or result['to']}")
    if result["backup"]:
        print(f"迁移前备份  : {result['backup']}")
    return 0


def cmd_plugin_status(args) -> int:
    """插件运行状态：启用列表、7 天崩溃次数、是否处于安全模式。"""
    from .core.plugins import PluginManager, crash_count

    mgr = PluginManager()
    plugins = mgr.scan()
    state = mgr._load()  # noqa: SLF001 - 只读展示
    print(f"插件目录    : {mgr.folder}")
    print(f"安全模式    : {'是（全部插件已禁用）' if mgr.safe_mode else '否'}")
    if mgr.safe_mode and state.get("safe_mode_reason"):
        print(f"触发原因    : {state['safe_mode_reason']}")
    if not plugins:
        print("（未安装插件，把 manifest.json + entry.py 放进 "
              f"{mgr.folder}/<插件ID>/）")
        return 0
    print()
    for plugin in plugins:
        mark = "启用" if mgr.is_enabled(plugin.id) else "停用"
        crashes = crash_count(state, plugin.id)
        perms = ", ".join(plugin.permissions) or "-"
        print(f"{plugin.id:<20} {plugin.manifest.name:<16} v{plugin.manifest.version} "
              f"{mark} 崩溃{crashes}/7天 权限：{perms}")
    for path in state.get("snapshots") or []:
        print(f"可回滚快照 : {path}")
    return 0


def cmd_plugin_toggle(args) -> int:
    """启用 / 停用插件；启用前自动打包快照，可随时回滚。"""
    from .core.plugins import PluginError, PluginManager

    mgr = PluginManager()
    try:
        result = mgr.set_enabled(args.id, enabled=bool(args.enable),
                                 cfg_file=_cfg(args).get("config_file") or "")
    except PluginError as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1
    print(f"{args.id} 已{'启用' if args.enable else '停用'}")
    if result.get("snapshot"):
        print(f"快照      : {result['snapshot']}")
    return 0


def cmd_plugin_rollback(args) -> int:
    """回滚到最近一次插件快照（配置 + 插件目录），同时退出安全模式。"""
    from .core.plugins import PluginManager

    mgr = PluginManager()
    cfg = _cfg(args)
    cfg_file = str(cfg.get("config_file") or "")
    cfg_dir = os.path.dirname(cfg_file) if cfg_file else ""
    snapshot = mgr.rollback(cfg_dir)
    if not snapshot:
        print("没有可回滚的快照", file=sys.stderr)
        return 1
    print(f"已回滚      : {snapshot}")
    print("安全模式已退出；重新启动程序即可加载回滚后的插件。")
    return 0


def cmd_hermes_token(args) -> int:
    """查看 / 轮换 Hermes 服务 token（存加密配置）。"""
    from .core.hermes import ensure_token, token_path

    token = ensure_token(rotate=bool(args.rotate))
    if not token:
        print("token 生成失败：请检查 ~/.chat2todo/hermes 权限", file=sys.stderr)
        return 1
    print(token)
    print(f"（保存在 {token_path()}，Windows 下用 DPAPI 加密）")
    if args.rotate:
        print("旧 token 已失效。")
    return 0


def cmd_hermes_serve(args) -> int:
    """以 CLI 方式起 Hermes 服务（不使用 Qt），供排障与自测。"""
    from .core.hermes import HermesService, MemoryHost

    cfg = _cfg(args)
    host = MemoryHost(cfg["db_path"], cfg)
    service = HermesService(host, port=args.port)
    url = service.start()
    if not url:
        print("Hermes 服务启动失败", file=sys.stderr)
        return 1
    print(f"服务地址    : {url}")
    print(f"token       : {service.token}")
    print("指令示例    : POST /command  {\"command\": \"todos\"}")
    print("按 Ctrl+C 结束")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        service.stop()
    return 0


def cmd_hermes_summary(args) -> int:
    """生成当日汇总；--out 写入文件，否则打印到终端。"""
    from .core.hermes import daily_summary

    cfg = _cfg(args)
    store = Store(cfg["db_path"])
    try:
        text = daily_summary(store, cfg)
    finally:
        store.close()
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            print(f"写入失败：{exc}", file=sys.stderr)
            return 1
        print(f"已写入      : {args.out}")
        return 0
    print(text, end="")
    return 0


def cmd_sbom(args) -> int:
    """生成 SBOM（sbom.json）与许可证清单（licenses.md）。"""
    from .core.sbom import write_all

    project = args.project or ""
    if not project:
        project = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "pyproject.toml")
    if not os.path.isfile(project):
        print(f"找不到 pyproject.toml：{project}", file=sys.stderr)
        return 1
    bom, lic = write_all(args.out, project)
    print(f"SBOM        : {bom}")
    print(f"许可证清单  : {lic}")
    return 0


def cmd_purge_chat(args) -> int:
    """按群清理历史：--list 看有哪些来源群，--chat 指定清理。"""
    store = Store(_db(args))
    try:
        if args.list:
            rows = store.list_chats()
            if not rows:
                print("库里还没有消息。")
                return 0
            names = store.chat_name_map()
            print(f"{_pad('chat_id', 22)}{_pad('群名', 20)}{'消息':>7}{'最早':>13}{'最晚':>13}")
            for r in rows:
                name = r["chat_id"] or "(未标记)"
                print(f"{_pad(name, 22)}{_pad(names.get(r['chat_id'], ''), 20)}{r['n']:>7}"
                      f"{_fmt_ts(r['first_ts']):>13}{_fmt_ts(r['last_ts']):>13}")
            return 0
        chat = (args.chat or "").strip()
        if not chat:
            print("用法：c2t purge-chat --list 查看来源群；"
                  "c2t purge-chat --chat <群号> [--dry-run]")
            return 2
        counts = store.chat_counts(chat)
        if args.dry_run:
            print(f"[预览] {chat}：消息 {counts['messages']} 条，待办 {counts['todos']} 条"
                  f"（加 --keep-todos 则只删消息）")
            return 0
        if not counts["messages"]:
            print(f"{chat} 在库里没有消息：老数据可能还没回填来源群，"
                  "先跑一次 db-import 再试")
            return 1
        res = store.purge_chat(chat, with_todos=not args.keep_todos)
    finally:
        store.close()
    print(f"已清理 {chat}：消息 {res['messages']} 条，待办 {res['todos']} 条")
    return 0


def cmd_purge_unmarked(args) -> int:
    """清理未标记来源群的消息（txt 导入的老数据）；--dry-run 只预览。"""
    store = Store(_db(args))
    try:
        counts = store.unmarked_counts()
        if not counts["messages"]:
            print("库里没有未标记来源群的消息。")
            return 0
        if args.dry_run:
            print(f"[预览] 未标记消息 {counts['messages']} 条；"
                  "清理只删消息，已抽出的待办保留")
            return 0
        res = store.purge_unmarked()
    finally:
        store.close()
    print(f"已清理未标记消息 {res['messages']} 条（待办未动）")
    return 0


def cmd_export_calibration(args) -> int:
    """导出校准档案到 JSON（项目专属 schema c2t-calibration-v1）。"""
    store = Store(_db(args))
    try:
        n = store.export_calibration(args.path)
    finally:
        store.close()
    print(f"已导出 {n} 条校准反馈到 {args.path}")
    return 0


def cmd_import_calibration(args) -> int:
    """从 JSON 导入校准档案（schema 不匹配会拒绝）。"""
    store = Store(_db(args))
    try:
        result = store.import_calibration(args.path)
    except ValueError as exc:
        print(f"跳过：{exc}")
        return 1
    finally:
        store.close()
    st = result["total"]
    print(f"已新增 {result['inserted']} 条校准反馈"
          f"（共 {st.get('total', 0)} 条，已存在或 schema 不匹配的已跳过）")
    return 0


def cmd_export_chatlab(args) -> int:
    """把库里全部消息导出成 ChatLab Format（JSON 或 JSONL）。"""
    from .adapters import ChatLabJsonAdapter

    store = Store(_db(args))
    try:
        msgs = store.all_messages()
    finally:
        store.close()

    adapter = ChatLabJsonAdapter()
    n = adapter.write(args.path, msgs)
    print(f"已导出 {n} 条消息到 {args.path}（ChatLab Format）")
    return 0


def cmd_serve(args) -> int:
    """启动本地 HTTP API（仅 127.0.0.1）。"""
    from .api import serve

    try:
        serve(_db(args), host=args.host, port=args.port, verbose=args.verbose)
    except OSError as exc:
        print(f"启动失败：{exc}（端口 {args.port} 可能已被占用）")
        return 1
    return 0


def _pipeline(path: str, cfg: dict, encoding: str = "") -> tuple:
    """把一份导出文件跑完 读→去重→抽取→跨群合并→语义→AI 清洗。

    返回 (messages, todos, decisions, llm_records)。
    """
    src = path
    tmp_src = ""
    if encoding:
        src = tmp_src = _decode_to_temp(path, encoding)
    name = _guess_adapter(path)
    if name not in REGISTRY:
        raise C2TError("E1002", f"没有名为 {name} 的适配器")
    raw = []
    try:
        raw = list(get_adapter(name).read(src))
    finally:
        _drop_temp(tmp_src)
    messages = dedupe(raw, strict=bool(cfg.get("strict_dedupe")))
    session = os.path.splitext(os.path.basename(path))[0]
    todos, decisions, llm_records, _ = _run_extraction(messages, cfg, session)
    return messages, todos, decisions, llm_records


def cmd_watch(args) -> int:
    """盯着一个文件夹：有新导出的聊天记录就自动导入并清洗。"""
    import json as _json
    import time as _time

    cfg = _cfg(args)
    folder = args.dir or os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.isdir(folder):
        raise C2TError("E1001", f"文件夹不存在：{folder}")
    state_file = os.path.join(os.path.expanduser("~"), ".c2t_watch.json")
    seen = {}
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as fh:
                seen = _json.load(fh)
        except Exception:
            seen = {}

    exts = (".txt", ".mht", ".html", ".jsonl", ".json")
    print(f"监视中：{folder}（每 {args.interval} 秒检查一次，Ctrl+C 退出）")
    while True:
        try:
            for name in os.listdir(folder):
                if not name.lower().endswith(exts):
                    continue
                path = os.path.join(folder, name)
                if not os.path.isfile(path):
                    continue
                stat = os.stat(path)
                stamp = f"{stat.st_size}-{int(stat.st_mtime)}"
                if seen.get(name) == stamp:
                    continue
                try:
                    messages, todos, decisions, llm_records = _pipeline(path, cfg, args.encoding)
                    store = Store(_db(args))
                    try:
                        added = store.add_messages(messages)
                        tadded = store.add_todos(todos)
                        store.add_decisions(decisions)
                        store.add_llm_calls(llm_records)
                    finally:
                        store.close()
                    print(f"[{_time.strftime('%H:%M:%S')}] {name}: 消息 {len(messages)}"
                          f"（新增 {added}）→ 待办 {len(todos)}（新增 {tadded}）")
                    seen[name] = stamp
                    with open(state_file, "w", encoding="utf-8") as fh:
                        _json.dump(seen, fh, ensure_ascii=False)
                except Exception as exc:  # noqa: BLE001 - 单个文件失败不中断监视
                    print(f"[{_time.strftime('%H:%M:%S')}] {name}: 失败 {exc}")
                    seen[name] = stamp
        except KeyboardInterrupt:
            print("已退出监视")
            return 0
        _time.sleep(max(2, int(args.interval)))


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
    p.add_argument("--status", default=None,
                   choices=["pending", "unconfirmed", "done", "noise", "muted"])
    p.add_argument("-v", "--verbose", action="store_true", help="同时显示判定依据")
    p.set_defaults(func=cmd_todos)

    p = sub.add_parser("done", help="标记待办完成")
    p.add_argument("id", type=int)
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("mute", help="此类消息以后不再提醒")
    p.add_argument("id", type=int, help="待办编号（todos 表里的 id）")
    p.add_argument("--scope", default="kind", choices=["kind", "sender", "source"],
                   help="kind=此类消息（默认，按语义相似度）；"
                        "sender=只静音这个人；source=整个群/整份导出")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_mute)

    p = sub.add_parser("unmute", help="撤销一条静音规则")
    p.add_argument("id", type=int, help="规则编号（c2t mutes 里看到的）")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_unmute)

    p = sub.add_parser("mutes", help="列出当前静音规则")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_mutes)

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

    p = sub.add_parser("explain", help="打印某条待办的完整判定报告")
    p.add_argument("todo_id", type=int, help="待办编号（todos 表里的 id）")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_explain)

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

    p = sub.add_parser("dash", help="打开置顶待办看板（tkinter 版，零依赖）")
    p.add_argument("--db", default="")
    p.add_argument("--interval", type=int, default=30, help="自动刷新秒数")
    p.add_argument("--status", default="pending", choices=["pending", "unconfirmed", "done"])
    p.set_defaults(func=cmd_dash)

    p = sub.add_parser("dash-qt", help="打开 Qt 版看板（托盘/玻璃/监听，需 .[qt]）")
    p.set_defaults(func=cmd_dash_qt)

    p = sub.add_parser("db", help="检查数据库只读通道（引擎 / 密钥 / 试读）")
    p.set_defaults(func=cmd_db)
    p = sub.add_parser("db-key", help="管理本机保存的数据库口令（DPAPI 加密）")
    p.add_argument("--save", action="store_true", help="交互输入口令，验证后加密保存")
    p.add_argument("--check", action="store_true", help="查看各来源口令状态")
    p.add_argument("--delete", action="store_true", help="删除本机保存的口令")
    p.add_argument("--force", action="store_true", help="验证不通过也强制保存")
    p.set_defaults(func=cmd_db_key)
    p = sub.add_parser("db-import", help="从 QQ 数据库只读拉取消息并抽取待办")
    p.add_argument("--db", default="")
    p.add_argument("--limit", type=int, default=0, help="每张表最多取 N 条，0 表示不限")
    p.add_argument("--days", type=int, default=0, help="只取最近 N 天，0 表示全部")
    p.add_argument("--session", default="")
    p.set_defaults(func=cmd_db_import)

    p = sub.add_parser("db-backfill", help="给历史消息回填来源群（不跑抽取、不调用 AI）")
    p.add_argument("--days", type=int, default=0, help="只回填最近 N 天，0 表示全部")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_db_backfill)

    p = sub.add_parser("backup", help="立即备份一次数据库（默认 DPAPI 加密）")
    p.add_argument("--dir", default="", help="备份目录，默认库文件同目录下的 backups/")
    p.add_argument("--keep", type=int, default=7, help="保留最近 N 份（默认 7）")
    p.add_argument("--no-encrypt", action="store_true", help="不加密（仅调试用）")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("wipe", help="一键清空程序生成内容（数据库 / 归档 / 备份 / 日志 / 缓存）")
    p.add_argument("--yes", action="store_true", help="确认执行；不加只打印范围")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_wipe)

    p = sub.add_parser("obsidian-sync", help="与 Obsidian 库双向同步待办（需先配置库路径）")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_obsidian_sync)

    p = sub.add_parser("desktop-export", help="输出脱敏待办 JSON/ICS（桌面美化联动）")
    p.add_argument("--dir", default="", help="输出目录，默认库文件同目录下的 desktop/")
    p.add_argument("--serve", action="store_true", help="同时启动本地回写接口，Ctrl+C 结束")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_desktop_export)

    p = sub.add_parser("desktop-token", help="查看 / 轮换桌面联动回写 token")
    p.add_argument("--rotate", action="store_true", help="立即轮换，旧 token 失效")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_desktop_token)

    p = sub.add_parser("purge-chat", help="按来源群清理历史消息（可一并删对应待办）")
    p.add_argument("--chat", default="", help="群号 / 会话号，先用 --list 查看")
    p.add_argument("--list", action="store_true", help="列出库里各来源群及条数")
    p.add_argument("--dry-run", action="store_true", help="只预览，不删除")
    p.add_argument("--keep-todos", action="store_true", help="只删消息，保留待办")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_purge_chat)

    p = sub.add_parser("purge-unmarked", help="清理未标记来源群的消息（txt 导入的老数据）")
    p.add_argument("--dry-run", action="store_true", help="只预览，不删除")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_purge_unmarked)

    p = sub.add_parser("export-calibration", help="导出校准档案到 JSON（项目专属 schema）")
    p.add_argument("path", help="目标 JSON 文件路径")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_export_calibration)

    p = sub.add_parser("import-calibration", help="从 JSON 导入校准档案")
    p.add_argument("path", help="JSON 档案路径")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_import_calibration)

    p = sub.add_parser("export-chatlab", help="导出全部消息为 ChatLab Format")
    p.add_argument("path", help="输出文件路径（.json 或 .jsonl）")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_export_chatlab)

    p = sub.add_parser("watch", help="监视文件夹，自动导入新导出的聊天记录")
    p.add_argument("--dir", default="", help="默认监视桌面")
    p.add_argument("--interval", type=int, default=5, help="检查间隔秒数")
    p.add_argument("--db", default="")
    p.add_argument("--encoding", default="")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("plugins", help="扫描 tools/* 下的插件 manifest 打表")
    p.set_defaults(func=cmd_plugins)

    p = sub.add_parser("plugin-status", help="插件运行状态：启用列表 / 崩溃次数 / 安全模式")
    p.set_defaults(func=cmd_plugin_status)

    p = sub.add_parser("migrate", help="数据库 schema 迁移（迁移前自动备份）")
    p.add_argument("--dry-run", action="store_true", help="只看版本，不做改动")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_migrate)

    for _name, _enable in (("plugin-enable", True), ("plugin-disable", False)):
        p = sub.add_parser(_name, help=f"{'启用' if _enable else '停用'}已安装插件")
        p.add_argument("id", help="插件 ID（plugin-status 里能看到）")
        p.add_argument("--db", default="")
        p.set_defaults(func=cmd_plugin_toggle, enable=_enable)

    p = sub.add_parser("plugin-rollback", help="回滚到最近一次插件快照并退出安全模式")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_plugin_rollback)

    p = sub.add_parser("hermes-token", help="查看 / 轮换 Hermes 服务 token")
    p.add_argument("--rotate", action="store_true", help="立即轮换，旧 token 失效")
    p.set_defaults(func=cmd_hermes_token)

    p = sub.add_parser("hermes-serve", help="以 CLI 方式启动 Hermes 服务（排障用）")
    p.add_argument("--port", type=int, default=0, help="0 = 随机端口（只绑 127.0.0.1）")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_hermes_serve)

    p = sub.add_parser("hermes-summary", help="生成当日汇总（消息数 / 待办清单）")
    p.add_argument("--out", default="", help="写入文件，留空则打印到终端")
    p.add_argument("--db", default="")
    p.set_defaults(func=cmd_hermes_summary)

    p = sub.add_parser("sbom", help="生成 SBOM 与第三方许可证清单")
    p.add_argument("--out", default="dist/sbom", help="输出目录，默认 dist/sbom")
    p.add_argument("--project", default="", help="pyproject.toml 路径，默认自动定位")
    p.set_defaults(func=cmd_sbom)

    p = sub.add_parser("serve", help="启动本地 HTTP API（仅本机 127.0.0.1）")
    p.add_argument("--host", default="127.0.0.1",
                   choices=["127.0.0.1", "localhost", "::1"],
                   help="监听地址，仅允许回环地址；API 无鉴权，不要暴露到局域网")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--db", default="")
    p.add_argument("--verbose", action="store_true", help="打印每个请求的访问日志")
    p.set_defaults(func=cmd_serve)

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
    # 默认只放行 WARNING 以上；设 C2T_DEBUG=1 可看到被容错吞掉的底层原因
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("C2T_DEBUG") else logging.WARNING,
        format="[%(levelname)s] %(name)s: %(message)s",
    )
    # 日志脱敏：手机号 / 身份证 / 邮箱在落盘前统一打码
    try:
        install_log_filter(load())
    except ConfigError:
        install_log_filter({})
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
