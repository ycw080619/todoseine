"""把 messages.jsonl 灌进 Chat2Todo：去重 → 规则抽取 → 本地 Ollama 复核 → 落库。

用法：
    python pipeline.py messages.jsonl --db C:/Users/you/Chat2Todo/qq.db --out C:/Users/you/Chat2Todo

默认关闭 rewrite（改写会把原句改得面目全非，人工无法核对），只开 review + intent。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from chat2todo.ai import AI  # noqa: E402
from chat2todo.config import load  # noqa: E402
from chat2todo.core import Store, dedupe, extract_todos  # noqa: E402
from chat2todo.core.sink_markdown import render_markdown  # noqa: E402
from chat2todo.models import Message  # noqa: E402


def read_jsonl(path: str):
    msgs = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = (row.get("content") or "").strip()
            if not content:
                continue
            msgs.append(Message(
                timestamp=int(row.get("timestamp") or 0),
                sender_id=str(row.get("sender") or ""),
                sender_name=str(row.get("sender_name") or row.get("sender") or ""),
                content=content,
            ))
    return msgs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl")
    ap.add_argument("--db", default=os.path.expanduser("~/Chat2Todo/qq.db"))
    ap.add_argument("--out", default="")
    ap.add_argument("--config", default="")
    args = ap.parse_args()

    cfg = load(args.config or None)
    ai = AI(cfg)

    msgs = dedupe(read_jsonl(args.jsonl))
    print(f"读到消息 {len(msgs)}")

    todos = extract_todos(msgs, session="qq")
    print(f"规则待办 {len(todos)}")

    if ai.on("review"):
        kept = [t for t in todos if ai.review(t.text) is not False]
        print(f"AI 复核保留 {len(kept)}（丢弃 {len(todos) - len(kept)}）")
        todos = kept
    if ai.on("intent"):
        kept = [t for t in todos if ai.intent(t.text) is not False]
        print(f"意图过滤保留 {len(kept)}")
        todos = kept

    store = Store(args.db)
    try:
        added = store.add_messages(msgs)
        tadd = store.add_todos(todos)
        all_todos = store.list_todos(None)
        stats = store.stats()
    finally:
        store.close()

    print(f"入库：消息新增 {added} / 待办新增 {tadd}")
    print(f"库内：{stats}")

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        md = render_markdown(all_todos, title="QQ 消息待办")
        (out / "todos_qq.md").write_text(md, encoding="utf-8")
        print(f"已写出 {out / 'todos_qq.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
