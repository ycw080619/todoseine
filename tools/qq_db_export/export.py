"""从已解密的 NTQQ 数据库导出 JSONL（在容器内运行）。

用法（容器内）：
    python export.py /data/plain.db /data/messages.jsonl [天数]

输出每行一条：
    {"timestamp": 1792842363, "sender": "231458000", "sender_name": "小李",
     "content": "明天前把周报发我", "type": 0, "chat_id": "670649801730901864"}

说明：
- 正文在 40800 的 protobuf BLOB 里，没有 proto 定义，这里取「最长的可打印 UTF-8 片段」；
- 会剥掉片段开头粘连的类型残渣（如 "6照片…" 里的 6、"!那你…" 里的 !）；
- 发送者昵称取 40093，会话 ID 取 40002，时间取 40050。
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time

# 开头常见的粘连残渣：数字或标点紧贴中文
JUNK_PREFIX = re.compile(r"^[\dA-Za-z=:：!！?？,，.。;；\-—_]{1,3}(?=[\u4e00-\u9fff])")
NOISE = re.compile(r"(/download\?|https?://\S{0,40}$|^\[图片\]$|^\[表情\]$)")


def longest_text(blob: bytes) -> str:
    if not blob:
        return ""
    text = blob.decode("utf-8", "ignore")
    # 控制字符当分隔
    parts = re.split(r"[\x00-\x08\x0b-\x1f\x7f]+", text)
    best = ""
    for part in parts:
        part = part.strip()
        if len(part) > len(best):
            best = part
    best = JUNK_PREFIX.sub("", best).strip()
    return best


def dump(conn: sqlite3.Connection, table: str, days: int, out) -> int:
    since = int(time.time()) - days * 86400 if days > 0 else 0
    cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    if not {"40050", "40800"} <= cols:
        return 0
    sql = (
        'SELECT "40001", "40002", "40021", "40027", "40050", "40093", "40800" '
        f'FROM "{table}" WHERE "40050" >= ? ORDER BY "40050" DESC'
    )
    n = 0
    for msg_id, chat_id, s1, s2, ts, nick, body in conn.execute(sql, (since,)):
        content = longest_text(body)
        if len(content) < 2 or len(content) > 400:
            continue
        if NOISE.search(content):
            continue
        sender = str(s2 or s1 or "")
        out.write(json.dumps({
            "timestamp": int(ts or 0),
            "sender": sender,
            "sender_name": str(nick or sender),
            "content": content,
            "type": 0,
            "chat_id": str(chat_id or ""),
            "msg_id": str(msg_id or ""),
        }, ensure_ascii=False) + "\n")
        n += 1
    return n


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "/data/plain.db"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "/data/messages.jsonl"
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    total = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for table in ("group_msg_table", "c2c_msg_table"):
            try:
                n = dump(conn, table, days, out)
            except sqlite3.Error as exc:
                print(f"[warn] {table}: {exc}", file=sys.stderr)
                continue
            print(f"{table}: {n}")
            total += n
    conn.close()
    print(f"total: {total} -> {out_path}")


if __name__ == "__main__":
    main()
