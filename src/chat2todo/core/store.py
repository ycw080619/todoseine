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
        self.conn.commit()

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

    def stats(self) -> dict:
        return {
            "messages": self._count("messages"),
            "todos": self._count("todos"),
            "pending": self.conn.execute(
                "SELECT COUNT(*) FROM todos WHERE status='pending'"
            ).fetchone()[0],
        }

    def _count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
