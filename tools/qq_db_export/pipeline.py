"""把 messages.jsonl 灌进 Chat2Todo：去重 → 规则抽取 → 语义合并/过滤 → 本地 AI 复核 → 落库。

用法：
    python pipeline.py messages.jsonl --db C:/Users/you/Chat2Todo/qq.db --out C:/Users/you/Chat2Todo

默认关闭 rewrite（改写会把原句改得面目全非，人工无法核对），只开 review + intent。

语义层（可选，默认自动启用）：用本机 Ollama 的 bge-m3 做
  1) 语义去重——措辞不同但同一件事，合并为一条；
  2) 公告过滤——与"群公告/通知"种子句相似的，直接剔除；
  3) 来源加权——私聊比群聊更可能是"找你办事"，群聊门槛更高。
向量层不可用时自动跳过，退回纯规则结果。
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
from chat2todo.core.vector import (  # noqa: E402
    ANNOUNCEMENT_SEEDS,
    Embedder,
    cluster,
    cosine,
)
from chat2todo.models import Message  # noqa: E402


def read_jsonl(path: str):
    """返回 (消息列表, 来源表)。来源表按 (sender, ts) 索引。"""
    msgs = []
    source_of = {}
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
            msg = Message(
                timestamp=int(row.get("timestamp") or 0),
                sender_id=str(row.get("sender") or ""),
                sender_name=str(row.get("sender_name") or row.get("sender") or ""),
                content=content,
            )
            msgs.append(msg)
            source_of[(msg.sender_id, msg.timestamp)] = str(row.get("source") or "group")
    return msgs, source_of


def semantic_dedupe(todos, embedder: Embedder, threshold: float):
    """措辞不同但同一件事的，只留置信度最高的一条。"""
    if not todos:
        return todos, 0
    texts = [t.text for t in todos]
    vecs = embedder.embed(texts)
    if not vecs:
        return todos, 0
    groups = cluster(list(zip(texts, vecs)), threshold=threshold)
    kept = []
    removed = 0
    for group in groups:
        best = max(group, key=lambda i: todos[i].confidence)
        kept.append(todos[best])
        removed += len(group) - 1
    kept.sort(key=lambda t: -t.confidence)
    return kept, removed


def drop_announcements(todos, embedder: Embedder, threshold: float):
    """与群公告种子句相似度超阈值的，视为通知而不是"要你做的事"。"""
    if not todos:
        return todos, 0
    seed_vecs = embedder.embed(ANNOUNCEMENT_SEEDS)
    if not seed_vecs:
        return todos, 0
    vecs = embedder.embed([t.text for t in todos])
    if not vecs:
        return todos, 0
    kept = []
    dropped = 0
    for todo, vec in zip(todos, vecs):
        if max(cosine(vec, s) for s in seed_vecs) >= threshold:
            dropped += 1
            continue
        kept.append(todo)
    return kept, dropped


def apply_source_weight(todos, source_of, group_w: float, c2c_w: float, min_conf: float):
    """按来源调权，而不是按绝对门槛一刀切。

    群聊里"顺带一提"的比例更高，稍微打折；私聊默认是对你说的，稍微加权。
    调权后再用一个全局下限兜底，避免把整批都筛没。
    """
    kept = []
    for t in todos:
        src = source_of.get((t.sender, t.message_ts), "group")
        factor = c2c_w if src == "c2c" else group_w
        t.confidence = round(min(0.99, t.confidence * factor), 2)
        if t.confidence >= min_conf:
            kept.append(t)
    kept.sort(key=lambda t: -t.confidence)
    return kept, len(todos) - len(kept)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl")
    ap.add_argument("--db", default=os.path.expanduser("~/Chat2Todo/qq.db"))
    ap.add_argument("--out", default="")
    ap.add_argument("--config", default="")
    ap.add_argument("--max-ai", type=int, default=120, help="最多问模型多少条（默认 120）")
    ap.add_argument("--embed-model", default="bge-m3")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--sim-threshold", type=float, default=0.92, help="语义去重阈值")
    ap.add_argument("--ann-threshold", type=float, default=0.70, help="公告相似度阈值")
    ap.add_argument("--group-w", type=float, default=0.95, help="群聊权重系数")
    ap.add_argument("--c2c-w", type=float, default=1.05, help="私聊权重系数")
    ap.add_argument("--min-conf", type=float, default=0.5, help="调权后的全局下限")
    ap.add_argument("--no-semantic", action="store_true", help="关闭语义层，只用规则")
    args = ap.parse_args()

    cfg = load(args.config or None)
    ai = AI(cfg)
    embedder = Embedder(base_url=args.ollama, model=args.embed_model)
    semantic = not args.no_semantic

    msgs, source_of = read_jsonl(args.jsonl)
    msgs = dedupe(msgs)
    print(f"读到消息 {len(msgs)}")

    todos = extract_todos(msgs, session="qq")
    print(f"规则待办 {len(todos)}")

    if semantic:
        todos, merged = semantic_dedupe(todos, embedder, args.sim_threshold)
        print(f"语义去重合并 {merged} 条，剩 {len(todos)}")
        todos, dropped = drop_announcements(todos, embedder, args.ann_threshold)
        print(f"公告过滤剔除 {dropped} 条，剩 {len(todos)}")
        todos, low = apply_source_weight(todos, source_of, args.group_w, args.c2c_w, args.min_conf)
        print(f"来源加权剔除 {low} 条，剩 {len(todos)}")

    # 只对「拿不准」的条目问模型：低置信度 + 前 N 条，避免几千次调用把机器拖死
    if ai.on("review") and args.max_ai > 0:
        borderline = [t for t in todos if t.confidence < 0.9][: args.max_ai]
        rest = [t for t in todos if t.confidence >= 0.9]
        kept = [t for t in borderline if ai.review(t.text) is not False]
        print(f"AI 复核 {len(borderline)} 条（上限 {args.max_ai}），保留 {len(kept)}；高置信直接保留 {len(rest)}")
        todos = sorted(rest + kept, key=lambda t: -t.confidence)
    if ai.on("intent") and args.max_ai > 0:
        pool = [t for t in todos if t.confidence < 0.9][: args.max_ai]
        sure = [t for t in todos if t.confidence >= 0.9]
        kept = [t for t in pool if ai.intent(t.text) is not False]
        print(f"意图过滤 {len(pool)} 条，保留 {len(kept)}；高置信直接保留 {len(sure)}")
        todos = sorted(sure + kept, key=lambda t: -t.confidence)

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
