"""对本地库里已抽出的待办做一次 AI 复核，把噪声标成 noise。

用法（先小批量验证再全量）：
    python review.py --db C:\\Users\\ycw\\Chat2Todo\\qq.db --limit 30 --workers 8
    python review.py --db C:\\Users\\ycw\\Chat2Todo\\qq.db --all

设计要点：
- 并发调用本地 Ollama（qwen2.5:14b），每个请求只发一条待办文本；
- 失败/超时一律视为"保留"，宁可留着让人自己划线，也不静默丢；
- 结果写回 status：todo → pending，noise → noise（看板/导出会过滤 noise）。
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from chat2todo.ai import AI  # noqa: E402
from chat2todo.config import load  # noqa: E402
from chat2todo.core import Store  # noqa: E402

SYSTEM = (
    "你是个人助理，帮用户从群聊消息里挑出「需要收件人去做」的事。\n"
    "只回答一个词：YES / MAYBE / NO。\n"
    "YES：明确要求收件人交付东西、参加活动、提交材料、按时到场、办理某事。\n"
    "MAYBE：向收件人求助、提问、请求确认、邀请回应（需要他回话或配合，但不是硬任务）。\n"
    "NO：吐槽、玩梗、寒暄、技术讨论、广告推广、别人自己要做的事、纯信息播报。\n"
    "例：\n"
    "「@全体成员 明天周一提前10分钟到教室，带上本周实验报告」→ YES\n"
    "「到了就在群里回复一下」→ YES\n"
    "「余班长，那个报名表他说要弄到一页，能问下你怎么操作吗」→ MAYBE\n"
    "「谁给我推荐个机箱」→ MAYBE\n"
    "「准备回我的晋江正道了」→ NO\n"
    "「哈哈哈笑死」→ NO"
)


def classify(ai: AI, text: str) -> str:
    """返回 'yes' / 'maybe' / 'no'；调用失败保守返回 'maybe'。"""
    out = ai._chat(SYSTEM, text, max_tokens=8)  # noqa: SLF001 - 复用底层调用
    if not out:
        return "maybe"
    up = out.upper()
    if "YES" in up:
        return "yes"
    if "MAYBE" in up:
        return "maybe"
    return "no"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条，0 表示全部")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--all", action="store_true", help="连 pending 一起复核")
    ap.add_argument("--config", default=r"C:\Users\ycw\Chat2Todo\c2t.toml")
    args = ap.parse_args()

    cfg = load(args.config)
    ai = AI(cfg)
    if not ai.usable:
        print("AI 不可用：检查 c2t.toml 的 [ai] 配置")
        return 1

    store = Store(args.db)
    try:
        items = store.list_todos(None if args.all else "unconfirmed")
        if args.limit:
            items = items[: args.limit]
        print(f"待复核 {len(items)} 条（模型 {ai.model}）")

        kept = maybe = dropped = failed = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(classify, ai, it.text): it for it in items}
            for i, fut in enumerate(as_completed(futures), 1):
                item = futures[fut]
                try:
                    verdict = fut.result()
                except Exception:  # noqa: BLE001
                    verdict, failed = "maybe", failed + 1
                if verdict == "yes":
                    store.set_status(item.todo_id, "pending")
                    kept += 1
                elif verdict == "maybe":
                    store.set_status(item.todo_id, "unconfirmed")
                    maybe += 1
                else:
                    store.set_status(item.todo_id, "noise")
                    dropped += 1
                if i % 20 == 0:
                    print(f"  ... {i}/{len(items)}  确认 {kept} 待定 {maybe} 剔除 {dropped}")
        s = store.stats()
        print(f"完成：确认 {kept} / 待定 {maybe} / 剔除 {dropped} / 失败转待定 {failed}")
        print(f"库内：消息 {s['messages']} / 待办 {s['todos']} / 待处理 {s['pending']}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
