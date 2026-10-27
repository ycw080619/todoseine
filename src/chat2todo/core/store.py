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
    chat_id      TEXT NOT NULL DEFAULT '',
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
    reviewed   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (text, ts, sender)
);
CREATE INDEX IF NOT EXISTS idx_todos_status ON todos (status);

CREATE TABLE IF NOT EXISTS trash (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    session    TEXT NOT NULL DEFAULT '',
    sender     TEXT NOT NULL DEFAULT '',
    ts         INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'done',
    created_at TEXT NOT NULL DEFAULT '',
    done_at    TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL
);

-- 群号 → 群名（db-import 时从 QQ 的 group_info.db 顺带刷新，仅展示用）
CREATE TABLE IF NOT EXISTS chat_names (
    chat_id TEXT PRIMARY KEY,
    name    TEXT NOT NULL DEFAULT ''
);

-- 用户校准反馈：每次点「非待办/完成/恢复」都写一条，长这个用户专属权重表
CREATE TABLE IF NOT EXISTS feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id      INTEGER NOT NULL,
    chat_id      TEXT NOT NULL DEFAULT '',
    sender       TEXT NOT NULL DEFAULT '',
    reason_tags  TEXT NOT NULL DEFAULT '',
    confidence   REAL NOT NULL DEFAULT 0,
    user_action  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (todo_id) REFERENCES todos(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_lookup
    ON feedback (chat_id, sender, reason_tags);
"""


class Store:
    def __init__(self, path: str = "c2t.db") -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._fts = self._init_fts()
        self.conn.commit()

    def _migrate(self) -> None:
        """把老库补齐到当前 schema（只加列，不删不改，保证已有数据安全）。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(todos)")}
        if "reviewed" not in cols:
            # AI 复核过的条目置 1，增量复核只挑没复核过的，避免每次全量重跑
            self.conn.execute(
                "ALTER TABLE todos ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 0"
            )
            # 老库里已有的条目视为"已复核过"，否则升级后第一次会把历史全量重跑
            self.conn.execute("UPDATE todos SET reviewed = 1")
            self.conn.commit()
        if "done_at" not in cols:
            # 完成时间：用于「当天保留、第二天自动归档」的判定
            self.conn.execute(
                "ALTER TABLE todos ADD COLUMN done_at TEXT NOT NULL DEFAULT ''"
            )
            self.conn.commit()
        msg_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(messages)")}
        if "chat_id" not in msg_cols:
            # 来源群号：过滤只对新同步生效，历史记录靠重新同步回填
            self.conn.execute(
                "ALTER TABLE messages ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''"
            )
            self.conn.commit()
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id)"
        )
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
        """写入消息，返回真正新增的条数（重复的会被忽略）。

        已有记录若缺少 chat_id，会借这次同步顺带回填（历史数据也能补上来源群）。
        """
        rows = [
            (
                m.timestamp, m.sender_id, m.sender_name, m.content or "",
                int(m.type), len(m.content or ""), m.content_hash, session,
                m.chat_id or "",
            )
            for m in messages
        ]
        before = self._count("messages")
        self.conn.executemany(
            "INSERT INTO messages "
            "(ts, sender_id, sender_name, content, type, content_len, content_hash, "
            "session, chat_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (ts, sender_id, content_len, content_hash) DO UPDATE SET "
            "chat_id = excluded.chat_id WHERE messages.chat_id = ''",
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

    # --- 群名（仅展示用） ------------------------------------------
    def set_chat_names(self, names: dict) -> int:
        """刷新群名表（群号 → 群名），返回写入条数；空名字跳过。"""
        rows = [(str(k), str(v).strip()) for k, v in (names or {}).items()
                if str(k).strip() and str(v).strip()]
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT OR REPLACE INTO chat_names (chat_id, name) VALUES (?, ?)", rows)
        self.conn.commit()
        return len(rows)

    def chat_name_map(self) -> dict:
        """全部已知的 群号 → 群名 映射，给展示层把群号换成群名用。"""
        return {r[0]: r[1] for r in
                self.conn.execute("SELECT chat_id, name FROM chat_names")}

    # --- 按群清理 -------------------------------------------------
    def list_chats(self, limit: int = 50) -> List[dict]:
        """已入库消息按来源群聚合（消息多的排前面），用于挑选清理对象。"""
        rows = self.conn.execute(
            "SELECT chat_id, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
            "FROM messages GROUP BY chat_id ORDER BY n DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def _chat_todo_ids(self, chat_id: str) -> List[int]:
        """这些消息抽出的待办：同发送者（昵称或号）、同时间戳视为同一条源消息。"""
        rows = self.conn.execute(
            "SELECT t.id FROM todos t WHERE EXISTS ("
            "SELECT 1 FROM messages m WHERE m.chat_id = ? AND m.ts = t.ts "
            "AND (m.sender_name = t.sender OR m.sender_id = t.sender))",
            (chat_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def chat_counts(self, chat_id: str) -> dict:
        """清理前预览：这个群在库里有多少消息 / 待办。"""
        chat_id = (chat_id or "").strip()
        if not chat_id:
            return {"messages": 0, "todos": 0}
        n = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)
        ).fetchone()[0]
        return {"messages": n, "todos": len(self._chat_todo_ids(chat_id))}

    def purge_chat(self, chat_id: str, with_todos: bool = True) -> dict:
        """按群清理历史：删除该群的消息，可选一并删除由它们抽出的待办。

        待办直接删除（不进回收站）——这是主动清理噪声，不是误删恢复场景。
        返回实际删除条数。
        """
        chat_id = (chat_id or "").strip()
        if not chat_id:
            return {"messages": 0, "todos": 0}
        todo_ids = self._chat_todo_ids(chat_id) if with_todos else []
        if todo_ids:
            self.conn.executemany("DELETE FROM todos WHERE id = ?",
                                  [(i,) for i in todo_ids])
        cur = self.conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        removed = cur.rowcount
        self.conn.commit()
        if removed and self._fts:
            # FTS5 不支持按普通列删除，直接重建索引（万级消息也是毫秒级）
            self.conn.execute("DELETE FROM messages_fts")
            self._sync_fts()
        return {"messages": removed, "todos": len(todo_ids)}

    def unmarked_counts(self) -> dict:
        """未标记来源群的消息规模（txt 导入的老数据 chat_id 为空）。"""
        n = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ''"
        ).fetchone()[0]
        return {"messages": n}

    def purge_unmarked(self) -> dict:
        """清理未标记来源群的消息。

        不连带删待办：这些待办多由 txt 导入产生，删掉消息不影响已抽出的结果。
        返回实际删除条数。
        """
        cur = self.conn.execute("DELETE FROM messages WHERE chat_id = ''")
        removed = cur.rowcount
        self.conn.commit()
        if removed and self._fts:
            # FTS5 不支持按普通列删除，直接重建索引（万级消息也是毫秒级）
            self.conn.execute("DELETE FROM messages_fts")
            self._sync_fts()
        return {"messages": removed}

    def backfill_chat_ids(self, rows: Iterable[tuple]) -> int:
        """给历史消息补来源群：只更新 chat_id 为空的已有行，不新增消息。

        rows 每项为 (ts, sender_id, content_len, content_hash, chat_id)。
        """
        payload = [(chat, ts, sid, ln, h) for ts, sid, ln, h, chat in rows if chat]
        if not payload:
            return 0
        before = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id != ''"
        ).fetchone()[0]
        self.conn.executemany(
            "UPDATE messages SET chat_id = ? WHERE ts = ? AND sender_id = ? "
            "AND content_len = ? AND content_hash = ? AND chat_id = ''",
            payload,
        )
        self.conn.commit()
        after = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id != ''"
        ).fetchone()[0]
        return after - before

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
                done_at=(r["done_at"] if "done_at" in r.keys() else "") or "",
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

    @staticmethod
    def _today() -> str:
        from datetime import date

        return date.today().isoformat()

    def set_status(self, todo_id: int, status: str = "done") -> bool:
        cur = self.conn.execute(
            "UPDATE todos SET status=?, done_at=? WHERE id=?",
            (status, self._today() if status == "done" else "", todo_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def update_text(self, todo_id: int, text: str) -> bool:
        """修改待办内容文本（看板行尾「改」按钮 / 右键菜单「修改内容」）。"""
        cur = self.conn.execute(
            "UPDATE todos SET text=? WHERE id=?", (text, todo_id))
        self.conn.commit()
        return cur.rowcount > 0

    def unreviewed_todos(self, limit: int = 0) -> List[TodoItem]:
        """还没被 AI 复核过的待办（增量复核用，避免每次全量重跑）。"""
        sql = "SELECT * FROM todos WHERE reviewed = 0 ORDER BY ts DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [
            TodoItem(
                todo_id=r["id"], text=r["text"], session=r["session"], sender=r["sender"],
                message_ts=r["ts"], reason=r["reason"], confidence=r["confidence"],
                status=r["status"],
            )
            for r in self.conn.execute(sql).fetchall()
        ]

    def mark_reviewed(self, todo_id: int, status: Optional[str] = None) -> bool:
        """记录某条已被复核，可选同时改状态（pending / unconfirmed / noise）。

        关键：改状态的同时把用户纠偏信号写进 feedback 表——
        noise → rejected（这条被规则误抓）
        done → confirmed（规则抽对了）
        """
        if status:
            cur = self.conn.execute(
                "UPDATE todos SET reviewed=1, status=? WHERE id=?", (status, todo_id)
            )
        else:
            cur = self.conn.execute(
                "UPDATE todos SET reviewed=1 WHERE id=?", (todo_id,)
            )
        # 自动记录校准反馈
        if status in ("noise", "done"):
            action = "rejected" if status == "noise" else "confirmed"
            try:
                self.record_feedback(todo_id, action)
            except Exception:  # noqa: BLE001 - 校准记账不能阻塞主流程
                pass
        self.conn.commit()
        return cur.rowcount > 0

    # ---------- 用户校准反馈闭环 -------------------------------
    def record_feedback(self, todo_id: int, user_action: str) -> int:
        """写一条校准反馈：完成/非待办/恢复。返回写入行数。

        从 todos 反查 chat_id/sender/reason/confidence，入库。
        reason 里的标签分号拆成 reason_tags，方便按 reason 粒度算权重。
        """
        row = self.conn.execute(
            "SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        if not row:
            return 0
        # chat_id 从 messages 反查（待办存的是 ts+sender，消息里才有 chat_id）
        chat_id = ""
        try:
            mrow = self.conn.execute(
                "SELECT chat_id FROM messages WHERE ts=? AND sender_id=? LIMIT 1",
                (row["ts"], row["sender"])).fetchone()
            if mrow:
                chat_id = mrow["chat_id"] or ""
        except Exception:  # noqa: BLE001 - messages 可能不存在这个 ts/sender 组合
            pass
        reason_tags = row["reason"] or ""
        if reason_tags:
            # "★重点 · 行动词:需要" → 拆成 ["★重点 · 行动词:需要"]
            # 保留完整 reason 字符串（分号分隔的标签），extract 用的时候再解析
            pass
        created_at = datetime.now().isoformat(timespec="seconds")
        cur = self.conn.execute(
            "INSERT INTO feedback "
            "(todo_id, chat_id, sender, reason_tags, confidence, user_action, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (todo_id, chat_id, row["sender"], reason_tags,
             row["confidence"], user_action, created_at))
        self.conn.commit()
        return cur.rowcount

    def load_feedback_weights(self) -> dict:
        """把 feedback 表聚合成权重字典，给 extract.py score_message() 用。

        返回 {(chat_id, sender): {reason_key: adjustment}}，adjustment ∈ [-0.5, 0.5]
        规则：每个 (chat_id, sender, reason_key) 组合的 confirmed_rate - rejected_rate
        再缩放到 [-0.5, 0.5] 区间；总样本 < 3 的不输出（噪声太大）。

        注意：reason_key 是 reason 字符串里的分号分隔标签，每个标签单独算权重。
        """
        rows = self.conn.execute(
            "SELECT chat_id, sender, reason_tags, user_action "
            "FROM feedback WHERE chat_id != '' OR sender != ''").fetchall()
        # 先展平成每 (chat_id, sender, reason_tag) 一条
        # reason_tags 里每个分号分隔的 tag 单独计数
        pairs: dict = {}
        for r in rows:
            tags = [t.strip() for t in (r["reason_tags"] or "").split(";") if t.strip()]
            # 兼容老数据：整个 reason 也可能是一个标签
            if not tags and r["reason_tags"]:
                tags = [r["reason_tags"].strip()]
            # reason 里带 "通知全体" / "行动词:需要" 这样的子标签也拆出来
            fine_tags = []
            for t in tags:
                fine_tags.append(t)                              # 整条
                if "·" in t:
                    fine_tags.append(t.split("·")[-1].strip())   # 最后那段，如 "行动词:需要"
            for tag in set(fine_tags):
                if not tag:
                    continue
                key = (r["chat_id"], r["sender"], tag)
                c = pairs.setdefault(key, {"confirmed": 0, "rejected": 0})
                if r["user_action"] == "confirmed":
                    c["confirmed"] += 1
                elif r["user_action"] == "rejected":
                    c["rejected"] += 1
        # 聚合成权重字典（按 chat_id + sender 维度分组）
        out: dict = {}
        for (chat_id, sender, tag), counts in pairs.items():
            total = counts["confirmed"] + counts["rejected"]
            if total < 3:
                continue                                # 样本太少，忽略
            rate = counts["confirmed"] / total
            adj = round((rate - 0.5) * 1.0, 2)           # [-0.5, 0.5] 区间
            if abs(adj) < 0.05:
                continue                                # 权重太小，省一次查表
            ck = (chat_id, sender)
            out.setdefault(ck, {})[tag] = adj
        return out

    def calibration_stats(self) -> dict:
        """给控制台/CLI 展示：攒了多少条校准。"""
        try:
            rows = self.conn.execute(
                "SELECT user_action, COUNT(*) FROM feedback GROUP BY user_action").fetchall()
            total = self.conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
            unique_users = self.conn.execute(
                "SELECT COUNT(DISTINCT chat_id || '/' || sender) FROM feedback").fetchone()[0]
            return {r[0]: r[1] for r in rows} | {"total": total, "unique_keys": unique_users}
        except Exception:  # noqa: BLE001
            return {"total": 0, "unique_keys": 0}

    def export_calibration(self, path: str) -> int:
        """导出校准档案到 JSON。格式是这个项目专属的（有 schema 版本），别人读不懂。

        返回导出的 feedback 条数。"""
        import json
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "schema": "c2t-calibration-v1",
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "feedback": [dict(r) for r in self.conn.execute(
                    "SELECT * FROM feedback ORDER BY id").fetchall()],
                "chat_names": {r["chat_id"]: r["name"]
                               for r in self.conn.execute(
                                   "SELECT * FROM chat_names").fetchall()},
            }, fh, ensure_ascii=False, indent=2)
        return self.conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]

    def import_calibration(self, path: str) -> dict:
        """从 JSON 导入校准档案。返回合并后的统计。

        schema 版本不匹配时跳过导入。已存在的 feedback 记录（同 todo_id + action）不重复插入。
        """
        import json
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("schema") != "c2t-calibration-v1":
            raise ValueError(f"不识别的校准档案 schema：{data.get('schema')}")
        inserted = 0
        for row in data.get("feedback", []):
            # 同 todo_id + action 不重复
            cur = self.conn.execute(
                "SELECT id FROM feedback WHERE todo_id=? AND user_action=?",
                (row["todo_id"], row["user_action"])).fetchone()
            if cur:
                continue
            self.conn.execute(
                "INSERT INTO feedback (todo_id, chat_id, sender, reason_tags, "
                "confidence, user_action, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["todo_id"], row.get("chat_id", ""), row.get("sender", ""),
                 row.get("reason_tags", ""), row.get("confidence", 0),
                 row["user_action"], row.get("created_at", "")))
            inserted += 1
        for cid, name in data.get("chat_names", {}).items():
            self.conn.execute(
                "INSERT OR REPLACE INTO chat_names (chat_id, name) VALUES (?, ?)",
                (cid, name))
        self.conn.commit()
        return {"inserted": inserted, "total": self.calibration_stats()}

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

    def nearby_messages(self, ts: int, n: int = 4) -> List[dict]:
        """给看板"双击看原文"用：取时间上离 ts 最近的几条消息。"""
        rows = self.conn.execute(
            "SELECT id, ts, sender_name, content FROM messages "
            "ORDER BY ABS(ts - ?) LIMIT ?",
            (int(ts), int(n)),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_status_many(self, ids, status: str = "done") -> int:
        """批量改状态（看板批量操作）。"""
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        today = self._today() if status == "done" else ""
        self.conn.executemany(
            "UPDATE todos SET status=?, done_at=? WHERE id=?",
            [(status, today, i) for i in ids],
        )
        self.conn.commit()
        return len(ids)

    def trash_many(self, ids) -> int:
        """批量丢进回收站。"""
        n = 0
        for i in [int(x) for x in ids]:
            n += 1 if self.trash_item(i) else 0
        return n

    # --- 完成归档 / 回收站 ---------------------------------------
    def archive_completed(self, keep_date: Optional[str] = None) -> int:
        """把「不是今天完成的」done 条目移进回收站，返回条数。

        当天勾掉的条目留在列表里（划线沉底），第二天自动进回收站。
        """
        keep = keep_date or self._today()
        rows = self.conn.execute(
            "SELECT * FROM todos WHERE status='done' AND (done_at='' OR done_at < ?)",
            (keep,),
        ).fetchall()
        for r in rows:
            self.conn.execute(
                "INSERT INTO trash (text, session, sender, ts, reason, confidence, "
                "status, created_at, done_at, deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["text"], r["session"], r["sender"], r["ts"], r["reason"],
                 r["confidence"], r["status"], r["created_at"],
                 r["done_at"] if "done_at" in r.keys() else "", self._today()),
            )
            self.conn.execute("DELETE FROM todos WHERE id=?", (r["id"],))
        self.conn.commit()
        return len(rows)

    def trash_item(self, todo_id: int) -> bool:
        """手动把一条待办丢进回收站。"""
        r = self.conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        if not r:
            return False
        self.conn.execute(
            "INSERT INTO trash (text, session, sender, ts, reason, confidence, "
            "status, created_at, done_at, deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["text"], r["session"], r["sender"], r["ts"], r["reason"],
             r["confidence"], r["status"], r["created_at"],
             r["done_at"] if "done_at" in r.keys() else "", self._today()),
        )
        self.conn.execute("DELETE FROM todos WHERE id=?", (todo_id,))
        self.conn.commit()
        return True

    def list_trash(self, limit: int = 200) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trash ORDER BY deleted_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def count_trash(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM trash").fetchone()[0])

    def restore(self, trash_id: int) -> bool:
        """从回收站还原成待办（状态回到 pending）。"""
        r = self.conn.execute("SELECT * FROM trash WHERE id=?", (trash_id,)).fetchone()
        if not r:
            return False
        self.conn.execute(
            "INSERT OR IGNORE INTO todos (text, session, sender, ts, reason, "
            "confidence, status, created_at, done_at, reviewed) "
            "VALUES (?,?,?,?,?,?,'pending',?,'',1)",
            (r["text"], r["session"], r["sender"], r["ts"], r["reason"],
             r["confidence"], r["created_at"]),
        )
        self.conn.execute("DELETE FROM trash WHERE id=?", (trash_id,))
        self.conn.commit()
        return True

    def purge_trash(self, days: int = 30) -> int:
        """清掉回收站里超过 days 天的记录；days<=0 表示永久保留。"""
        if days is None or int(days) <= 0:
            return 0
        from datetime import date, timedelta

        cutoff = (date.today() - timedelta(days=int(days))).isoformat()
        cur = self.conn.execute("DELETE FROM trash WHERE deleted_at < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount or 0

    def clear_trash(self) -> int:
        cur = self.conn.execute("DELETE FROM trash")
        self.conn.commit()
        return cur.rowcount or 0

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

    def weekly_stats(self, now: Optional[datetime] = None) -> dict:
        """近 7 天摘要（控制台「每周摘要」卡用，纯查询不写库）。

        - new：近 7 天新增待办数（按源头消息时间 ts；手动添加的 ts=添加时刻）
        - done：近 7 天完成数（todos 里当天完成的 + 回收站里已归档的，按 done_at 日期比）
        - top_chat：(显示名, 消息条数)，近 7 天最活跃的群；chat_names 有名用名，否则号码
        """
        from datetime import timedelta

        now = now or datetime.now()
        since_ts = int((now - timedelta(days=7)).timestamp())
        since_day = (now - timedelta(days=7)).date().isoformat()
        new = self.conn.execute(
            "SELECT COUNT(*) FROM todos WHERE ts >= ?", (since_ts,)).fetchone()[0]
        done = self.conn.execute(
            "SELECT COUNT(*) FROM todos WHERE status='done' AND done_at >= ?",
            (since_day,)).fetchone()[0]
        done += self.conn.execute(
            "SELECT COUNT(*) FROM trash WHERE done_at >= ? AND done_at != ''",
            (since_day,)).fetchone()[0]
        row = self.conn.execute(
            "SELECT chat_id, COUNT(*) AS n FROM messages "
            "WHERE ts >= ? AND chat_id != '' GROUP BY chat_id ORDER BY n DESC LIMIT 1",
            (since_ts,)).fetchone()
        top_chat = None
        if row:
            name = self.chat_name_map().get(row["chat_id"])
            top_chat = (name or row["chat_id"], row["n"])
        return {"new": new, "done": done, "top_chat": top_chat}
