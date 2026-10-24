"""SQLite 存储层（标准库 sqlite3，无外部依赖）。

表结构刻意保持扁平，便于用户直接用 SQLite 工具查看或导出。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from ..models import Message, TodoItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,
    sender_id    TEXT NOT NULL,
    sender_name  TEXT NOT NULL,
    content      TEXT NOT NULL,
    type         INTEGER NOT NULL DEFAULT 0,
    content_len  INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    session      TEXT NOT NULL DEFAULT '',
    UNIQUE (ts, sender_id, content_len, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages (ts);

CREATE TABLE IF NOT EXISTS todos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    session    TEXT NOT NULL DEFAULT '',
    sender     TEXT NOT NULL DEFAULT '',
    ts         INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT '',
    UNIQUE (text, ts, sender)
);
CREATE INDEX IF NOT EXISTS idx_todos_status ON todos (status);
"""


class Store:
    def __init__(self, path: str = "c2t.db") -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._fts = self._init_fts()
        self.conn.commit()

    # --- 全文索引（P2） -------------------------------------------
    def _init_fts(self) -> bool:
        """优先建 trigram 分词 FTS5（对中文友好）；不支持则退回 LIKE 检索。"""
        for tokenizer in ("trigram", "unicode61"):
            try:
                self.conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
                    "content, sender_name, session, msg_id UNINDEXED, "
                    f"tokenize='{tokenizer}')"
                )
                return True
            except sqlite3.OperationalError:
                continue
        return False

    def _sync_fts(self) -> None:
        if not self._fts:
            return
        self.conn.execute(
            "INSERT INTO messages_fts (content, sender_name, session, msg_id) "
            "SELECT m.content, m.sender_name, m.session, m.id FROM messages m "
            "WHERE m.id NOT IN (SELECT msg_id FROM messages_fts)"
        )
        self.conn.commit()

    def search(self, keyword: str, limit: int = 20) -> List[dict]:
        """检索原文。先用 FTS，再用 LIKE 兜底（2 字中文查询 FTS 可能不命中）。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        rows: List[sqlite3.Row] = []
        if self._fts and len(keyword) >= 3:
            try:
                rows = self.conn.execute(
                    "SELECT m.* FROM messages_fts f JOIN messages m ON m.id = f.msg_id "
                    "WHERE messages_fts MATCH ? ORDER BY m.ts LIMIT ?",
                    (keyword, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE content LIKE ? OR sender_name LIKE ? "
                "ORDER BY ts LIMIT ?",
                (f"%{keyword}%", f"%{keyword}%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_message(self, msg_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
        return dict(row) if row else None

    def context(self, msg_id: int, before: int = 2, after: int = 2) -> List[dict]:
        """取某条消息的前后文（双视图的“原文”一侧）。"""
        row = self.get_message(msg_id)
        if not row:
            return []
        prev = self.conn.execute(
            "SELECT * FROM messages WHERE id < ? ORDER BY id DESC LIMIT ?", (msg_id, before)
        ).fetchall()
        nxt = self.conn.execute(
            "SELECT * FROM messages WHERE id > ? ORDER BY id ASC LIMIT ?", (msg_id, after)
        ).fetchall()
        return [dict(r) for r in reversed(prev)] + [row] + [dict(r) for r in nxt]

    def close(self) -> None:
        self.conn.close()

    # --- 写入 -----------------------------------------------------
    def add_messages(self, messages: Iterable[Message], session: str = "") -> int:
        """写入消息，返回真正新增的条数（重复的会被忽略）。"""
        rows = [
            (
                m.timestamp, m.sender_id, m.sender_name, m.content or "",
                int(m.type), len(m.content or ""), m.content_hash, session,
            )
            for m in messages
        ]
        before = self._count("messages")
        self.conn.executemany(
            "INSERT OR IGNORE INTO messages "
            "(ts, sender_id, sender_name, content, type, content_len, content_hash, session) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()
        self._sync_fts()
        return self._count("messages") - before

    def add_todos(self, todos: Iterable[TodoItem]) -> int:
        before = self._count("todos")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.executemany(
            "INSERT OR IGNORE INTO todos "
            "(text, session, sender, ts, reason, confidence, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [t.as_row() + (now,) for t in todos],
        )
        self.conn.commit()
        return self._count("todos") - before

    # --- 查询 -----------------------------------------------------
    def list_todos(self, status: Optional[str] = None) -> List[TodoItem]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM todos WHERE status = ? ORDER BY ts", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM todos ORDER BY ts").fetchall()
        return [
            TodoItem(
                todo_id=r["id"], text=r["text"], session=r["session"], sender=r["sender"],
                message_ts=r["ts"], reason=r["reason"], confidence=r["confidence"],
                status=r["status"],
            )
            for r in rows
        ]

    def all_messages(self) -> List[Message]:
        rows = self.conn.execute("SELECT * FROM messages ORDER BY ts").fetchall()
        return [
            Message(
                timestamp=r["ts"], sender_id=r["sender_id"], sender_name=r["sender_name"],
                content=r["content"], type=r["type"],
            )
            for r in rows
        ]

    def set_status(self, todo_id: int, status: str = "done") -> bool:
        cur = self.conn.execute(
            "UPDATE todos SET status=? WHERE id=?", (status, todo_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def add_todo(self, text: str, session: str = "手动", sender: str = "我") -> int:
        """手动添加一条待办（看板上的「＋ 添加」）。返回写入行数。"""
        now = int(datetime.now().timestamp())
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO todos "
            "(text, session, sender, ts, reason, confidence, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (text, session, sender, now, "手动添加", 1.0, ""),
        )
        self.conn.commit()
        return cur.rowcount

    def stats(self, since_ts: Optional[int] = None) -> dict:
        """统计。since_ts 给定时只统计该时间点之后的数据（c2t stats --since 7d）。"""
        if since_ts is None:
            return {
                "messages": self._count("messages"),
                "todos": self._count("todos"),
                "pending": self.conn.execute(
                    "SELECT COUNT(*) FROM todos WHERE status='pending'"
                ).fetchone()[0],
            }
        return {
            "messages": self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE ts >= ?", (since_ts,)
            ).fetchone()[0],
            "todos": self.conn.execute(
                "SELECT COUNT(*) FROM todos WHERE ts >= ?", (since_ts,)
            ).fetchone()[0],
            "pending": self.conn.execute(
                "SELECT COUNT(*) FROM todos WHERE status='pending' AND ts >= ?", (since_ts,)
            ).fetchone()[0],
        }

    def _count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
