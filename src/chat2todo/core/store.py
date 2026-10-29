"""SQLite 存储层（标准库 sqlite3，无外部依赖）。

表结构刻意保持扁平，便于用户直接用 SQLite 工具查看或导出。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from ..models import Message, TodoItem
from .vector import LocalEmbedder, cosine

logger = logging.getLogger("chat2todo.store")

# 「此类消息」判定：与静音样板的余弦相似度 ≥ 阈值就算同一类。
# 实测同批通知 0.31–0.63、无关消息 ≤0.12，取 0.25 留出安全余量；
# 过短的句子字面特征太少，不参与语义判定，避免误伤。
MUTE_SIM_THRESHOLD = 0.25
MUTE_MIN_CHARS = 10


def _load_vec(raw: str) -> List[float]:
    """把库里存的向量 JSON 还原成 float 列表；损坏或为空就当没有。"""
    if not raw:
        return []
    try:
        vec = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(vec, list):
        return []
    try:
        return [float(x) for x in vec]
    except (TypeError, ValueError):
        return []


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
    UNIQUE (ts, sender_id, content_len, content_hash, chat_id)
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

-- 静音规则：对某条通知点「以后不再提醒」后写一行。
--   text 非空  → 语义静音：之后与这句相似的消息都算「此类」，不再提醒
--   sender 非空 → 只静音这个来源里的这个人
--   session 非空且 sender 为空 → 整个来源（整个群/整份导出）静音
CREATE TABLE IF NOT EXISTS mutes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session    TEXT NOT NULL DEFAULT '',
    sender     TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL DEFAULT '',
    vector     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mutes_key ON mutes (session, sender, text);

-- 用户校准反馈：每次点「非待办/完成/恢复」都写一条，长这个用户专属权重表
-- todo_id 故意可空、且不建 FK：校准档案要能跨机器导入，落地时源库的 todo_id
-- 在本库多半不存在，硬约束会让整份档案导入失败。孤儿行由 _heal_feedback_orphans()
-- 在每次删待办后置 NULL（等价于 ON DELETE SET NULL 的效果）。
CREATE TABLE IF NOT EXISTS feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id      INTEGER,
    chat_id      TEXT NOT NULL DEFAULT '',
    sender       TEXT NOT NULL DEFAULT '',
    reason_tags  TEXT NOT NULL DEFAULT '',
    confidence   REAL NOT NULL DEFAULT 0,
    user_action  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_lookup
    ON feedback (chat_id, sender, reason_tags);

-- 决策日志：每条消息一个最终判定结果，便于追溯"为什么没进待办"
CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,
    sender_id    TEXT NOT NULL DEFAULT '',
    sender_name  TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    outcome      TEXT NOT NULL,       -- pending / unconfirmed / filtered / noise / deduped
    score        REAL NOT NULL DEFAULT 0,
    threshold    REAL NOT NULL DEFAULT 0,
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_lookup
    ON decisions (ts, sender_id, content_hash);

-- LLM 调用记录：每次 AI 调用一行，事后可算成本、查失败率
CREATE TABLE IF NOT EXISTS llm_calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    feature      TEXT NOT NULL,       -- review / intent / rewrite / merge / refine_ocr
    model        TEXT NOT NULL DEFAULT '',
    in_tokens    INTEGER NOT NULL DEFAULT 0,
    out_tokens   INTEGER NOT NULL DEFAULT 0,
    latency_ms   INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_feature ON llm_calls (feature);

-- 内部簿记（FTS 增量同步水位等），一个 key 一行
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


class Store:
    # bump 这个版本号时，_migrate 会对老库跑一遍结构补齐 + 孤儿解绑；
    # 当前版本没变就直接跳过，避免看板每 30 秒刷新都跑一次全表 UPDATE。
    SCHEMA_VERSION = "6"

    def __init__(self, path: str = "c2t.db") -> None:
        self.path = path
        # 默认库在 ~/Chat2Todo/ 下，首次使用目录还不存在，先补上再连
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # timeout=5 是 Python 层的 busy 等待；下面 busy_timeout=5000 是 SQLite
        # 层的，两道一起上。看板 / 控制台同步线程 / 计划任务三路同时打一个库，
        # 没有 WAL + busy_timeout 会随机抛 "database is locked"，又因为上层
        # 普遍 except: pass，用户只看到「看板不刷新」「同步没结果」。
        self.conn = sqlite3.connect(path, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        # WAL 模式下 NORMAL 就够：checkpoint 时才刷盘，写入性能和可靠性的平衡点
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # SQLite 默认不校验外键，必须每条连接显式打开；目前表结构里 feedback
        # 的解绑是显式 _heal_feedback_orphans()，但保留开关，新增关系表时才生效
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._fts = self._init_fts()
        self.conn.commit()

    def _migrate(self) -> None:
        """把老库补齐到当前 schema（只加列，不删不改，保证已有数据安全）。"""
        # 看板每 30 秒新建一个 Store，结构没变就跳过——尤其是里面的
        # _heal_feedback_orphans() 是全表 UPDATE ... NOT IN 子查询，库大了会卡。
        if self._meta_get("schema_version") == self.SCHEMA_VERSION:
            return
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
        self._rebuild_messages_chat_unique()
        self._migrate_mutes()
        self._rebuild_feedback_nullable()
        self._heal_feedback_orphans()
        self._meta_set("schema_version", self.SCHEMA_VERSION)
        self.conn.commit()

    def _migrate_mutes(self) -> None:
        """静音表从「按人/按来源」升级到「按语义」：补 text/vector 两列。

        唯一键也要从 (session, sender) 放宽到 (session, sender, text)，
        否则同一个来源下存不下第二条「此类消息」样板。
        """
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(mutes)")}
        if "text" not in cols:
            self.conn.execute(
                "ALTER TABLE mutes ADD COLUMN text TEXT NOT NULL DEFAULT ''")
        if "vector" not in cols:
            self.conn.execute(
                "ALTER TABLE mutes ADD COLUMN vector TEXT NOT NULL DEFAULT ''")
        self.conn.execute("DROP INDEX IF EXISTS idx_mutes_key")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_mutes_key "
            "ON mutes (session, sender, text)")
        self.conn.commit()

    def _rebuild_messages_chat_unique(self) -> None:
        """把 messages 的唯一键从 4 列放宽到 5 列（补 chat_id）。

        老库的 UNIQUE (ts, sender_id, content_len, content_hash) 会把
        「同一秒、同一人、同一句话、发到两个群」的第二条当成重复丢掉。
        唯一约束没法 ALTER，只能重建表；id 原样保留，FTS 增量水位不受影响。
        """
        uniques = []
        for idx in self.conn.execute("PRAGMA index_list(messages)").fetchall():
            if not idx[2]:      # 只看 UNIQUE 索引
                continue
            cols = [r[2] for r in
                    self.conn.execute(f"PRAGMA index_info('{idx[1]}')").fetchall()]
            if cols:
                uniques.append(cols)
        if any("chat_id" in cols for cols in uniques):
            return              # 已经是新结构

        self.conn.commit()
        self.conn.execute("""
            CREATE TABLE messages_new (
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
                UNIQUE (ts, sender_id, content_len, content_hash, chat_id)
            )
        """)
        self.conn.execute("""
            INSERT INTO messages_new
                (id, ts, sender_id, sender_name, content, type,
                 content_len, content_hash, session, chat_id)
            SELECT id, ts, sender_id, sender_name, content, type,
                   content_len, content_hash, session, chat_id
            FROM messages
        """)
        self.conn.execute("DROP TABLE messages")
        self.conn.execute("ALTER TABLE messages_new RENAME TO messages")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages (ts)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id)"
        )
        self.conn.commit()

    def _rebuild_feedback_nullable(self) -> None:
        """老库的 feedback.todo_id 是 NOT NULL，孤儿样本根本没法解绑。

        这里把表重建成可空（并去掉已失效的 FK），顺手把历史上指向不存在待办的
        行置 NULL —— 那些样本仍可用于校准，权重按 chat/sender/标签 聚合。
        """
        info = {r[1]: r for r in self.conn.execute("PRAGMA table_info(feedback)")}
        col = info.get("todo_id")
        if col is None or not col[3]:      # notnull=0，已是可空，无需处理
            return
        self.conn.executescript(
            """
            CREATE TABLE feedback_new (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                todo_id      INTEGER,
                chat_id      TEXT NOT NULL DEFAULT '',
                sender       TEXT NOT NULL DEFAULT '',
                reason_tags  TEXT NOT NULL DEFAULT '',
                confidence   REAL NOT NULL DEFAULT 0,
                user_action  TEXT NOT NULL,
                created_at   TEXT NOT NULL
            );
            INSERT INTO feedback_new
                (id, todo_id, chat_id, sender, reason_tags, confidence,
                 user_action, created_at)
            SELECT id, todo_id, chat_id, sender, reason_tags, confidence,
                   user_action, created_at
            FROM feedback;
            DROP TABLE feedback;
            ALTER TABLE feedback_new RENAME TO feedback;
            CREATE INDEX IF NOT EXISTS idx_feedback_lookup
                ON feedback (chat_id, sender, reason_tags);
            """
        )

    def _heal_feedback_orphans(self) -> None:
        """把指向已删待办的校准样本解绑（todo_id → NULL）。"""
        self.conn.execute(
            "UPDATE feedback SET todo_id = NULL "
            "WHERE todo_id IS NOT NULL "
            "AND todo_id NOT IN (SELECT id FROM todos)"
        )

    # --- 全文索引（P2） -------------------------------------------
    def _meta_get(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

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
            except sqlite3.OperationalError as exc:
                # 不静默：日志里能看到是分词器不支持还是 FTS5 整个不可用
                logger.debug("FTS5 tokenizer '%s' 不可用：%s", tokenizer, exc)
                continue
        logger.warning("SQLite 未启用 FTS5，全文检索退化为 LIKE（大库会慢）")
        return False

    def _fts_watermark(self) -> int:
        """已建索引的最大 messages.id，增量同步只补这之后的行。

        老库首次升级时 meta 里没有水位：此前 _sync_fts 是全量对齐的，
        所以已有索引就以当前最大消息 id 为准，避免重复灌一遍。
        """
        value = self._meta_get("fts_last_id")
        if value is not None:
            return int(value or 0)
        indexed = self.conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        last = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        watermark = int(last) if indexed else 0
        self._meta_set("fts_last_id", str(watermark))
        return watermark

    def _sync_fts(self) -> None:
        """增量补齐全文索引：只扫 id > 水位的消息，避免每次同步全表 diff。"""
        if not self._fts:
            return
        watermark = self._fts_watermark()
        cur = self.conn.execute(
            "INSERT INTO messages_fts (content, sender_name, session, msg_id) "
            "SELECT m.content, m.sender_name, m.session, m.id FROM messages m "
            "WHERE m.id > ?",
            (watermark,),
        )
        if cur.rowcount:
            last = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
            self._meta_set("fts_last_id", str(last))
        self.conn.commit()

    def _rebuild_fts(self) -> None:
        """整表重建索引（按群清理后用）：一次全扫，比逐行删快得多。"""
        if not self._fts:
            return
        self.conn.execute("DELETE FROM messages_fts")
        self._meta_set("fts_last_id", "0")
        self._sync_fts()

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
            # LIKE 通配符必须转义：搜 "%" 会匹配全部原文，一次拉走整库
            esc = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE content LIKE ? ESCAPE '\\' "
                "OR sender_name LIKE ? ESCAPE '\\' ORDER BY ts LIMIT ?",
                (f"%{esc}%", f"%{esc}%", limit),
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
        # 先把历史行缺失的 chat_id 补上：唯一的冲突键现在包含 chat_id，
        # 不先回填的话，老的空群号行和新行会被当成两条不同消息。
        self.conn.executemany(
            "UPDATE messages SET chat_id = ? "
            "WHERE ts = ? AND sender_id = ? AND content_len = ? AND content_hash = ? "
            "AND chat_id = '' AND ? != ''",
            [(r[8], r[0], r[1], r[5], r[6], r[8]) for r in rows],
        )
        self.conn.executemany(
            "INSERT INTO messages "
            "(ts, sender_id, sender_name, content, type, content_len, content_hash, "
            "session, chat_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (ts, sender_id, content_len, content_hash, chat_id) DO NOTHING",
            rows,
        )
        self.conn.commit()
        self._sync_fts()
        return self._count("messages") - before

    def add_todos(self, todos: Iterable[TodoItem]) -> int:
        before = self._count("todos")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # 命中静音规则的条目直接落成 muted：既不进列表也不提醒，
        # 但数据仍在，撤销静音后能原样恢复（比丢弃安全）。
        rules = self.muted_rules()
        seeds = self.semantic_seeds()
        payload = []
        for t in todos:
            row = list(t.as_row())
            if row[6] in ("pending", "unconfirmed") and (
                    self.is_muted(t.session, t.sender, rules)
                    or self.matches_seed(t.text, seeds)[0]):
                row[6] = "muted"
            payload.append(tuple(row) + (now,))
        self.conn.executemany(
            "INSERT OR IGNORE INTO todos "
            "(text, session, sender, ts, reason, confidence, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        self.conn.commit()
        return self._count("todos") - before

    # --- 决策日志 -------------------------------------------------
    def add_decisions(self, rows: Iterable[dict]) -> int:
        """批量写入决策日志。rows 每项含 ts/sender_id/sender_name/content_hash/
        outcome/score/threshold/reason。返回写入条数。"""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = [
            (
                int(r["ts"]), str(r.get("sender_id") or ""),
                str(r.get("sender_name") or ""), str(r.get("content_hash") or ""),
                str(r["outcome"]), float(r.get("score") or 0),
                float(r.get("threshold") or 0), str(r.get("reason") or ""), now,
            )
            for r in rows
        ]
        if not payload:
            return 0
        self.conn.executemany(
            "INSERT INTO decisions "
            "(ts, sender_id, sender_name, content_hash, outcome, score, "
            "threshold, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        self.conn.commit()
        return len(payload)

    def query_decision(self, ts: int, sender_id: str,
                       content_hash: str) -> Optional[dict]:
        """按消息定位它的判定结果（最近一条）。"""
        row = self.conn.execute(
            "SELECT * FROM decisions WHERE ts=? AND sender_id=? AND content_hash=? "
            "ORDER BY id DESC LIMIT 1",
            (int(ts), str(sender_id or ""), str(content_hash or "")),
        ).fetchone()
        return dict(row) if row else None

    # --- LLM 调用记录 ---------------------------------------------
    def add_llm_calls(self, rows: Iterable[dict]) -> int:
        """批量写入 LLM 调用记录。rows 每项含 feature/model/in_tokens/
        out_tokens/latency_ms/failed。返回写入条数。"""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = [
            (
                str(r["feature"]), str(r.get("model") or ""),
                int(r.get("in_tokens") or 0), int(r.get("out_tokens") or 0),
                int(r.get("latency_ms") or 0), int(r.get("failed") or 0), now,
            )
            for r in rows
        ]
        if not payload:
            return 0
        self.conn.executemany(
            "INSERT INTO llm_calls "
            "(feature, model, in_tokens, out_tokens, latency_ms, failed, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        self.conn.commit()
        return len(payload)

    def llm_stats(self) -> dict:
        """按 feature 聚合：调用数、失败数、平均延迟（ms）。"""
        rows = self.conn.execute(
            "SELECT feature, COUNT(*) AS n, SUM(failed) AS fails, "
            "AVG(latency_ms) AS avg_lat "
            "FROM llm_calls GROUP BY feature"
        ).fetchall()
        total = self.conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
        return {
            "total": total,
            "features": {
                r["feature"]: {
                    "calls": r["n"],
                    "failed": r["fails"] or 0,
                    "avg_latency_ms": round(r["avg_lat"] or 0, 1),
                }
                for r in rows
            },
        }

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
        if todo_ids:
            self._heal_feedback_orphans()      # 待办没了，对应校准样本解绑
        self.conn.commit()
        if removed and self._fts:
            # FTS5 不支持按普通列删除，直接重建索引（万级消息也是毫秒级）
            self._rebuild_fts()
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
            self._rebuild_fts()
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

    # ---------- 静音规则（以后不再提醒） ------------------------
    def muted_rules(self) -> list:
        """按人/按来源的规则对；语义规则（text 非空）不在这里，另有向量判定。"""
        return [(r["session"], r["sender"]) for r in self.conn.execute(
            "SELECT session, sender FROM mutes WHERE text = '' ORDER BY id").fetchall()]

    def semantic_seeds(self) -> List[tuple]:
        """语义静音样板：[(text, vector)]，vector 缺失时按 text 现算。"""
        rows = self.conn.execute(
            "SELECT text, vector FROM mutes WHERE text != '' ORDER BY id").fetchall()
        emb = LocalEmbedder()
        seeds = []
        for r in rows:
            vec = _load_vec(r["vector"])
            if not vec:
                vec = emb.embed([r["text"]])[0]
            seeds.append((r["text"], vec))
        return seeds

    @staticmethod
    def is_muted(session: str, sender: str, rules) -> bool:
        """规则为空串表示通配：session 空 = 任何来源，sender 空 = 该来源的所有人。"""
        s, who = (session or ""), (sender or "")
        for rs, rw in rules:
            if (not rs or rs == s) and (not rw or rw == who):
                return True
        return False

    @staticmethod
    def matches_seed(text: str, seeds) -> tuple:
        """这条待办是不是「此类消息」。返回 (命中, 相似度)。"""
        flat = " ".join((text or "").split())
        if not seeds or len(flat) < MUTE_MIN_CHARS:
            return False, 0.0
        query = LocalEmbedder().embed([flat])[0]
        best = max((cosine(query, vec) for _, vec in seeds), default=0.0)
        return best >= MUTE_SIM_THRESHOLD, best

    def mute(self, session: str, sender: str = "", text: str = "") -> dict:
        """新增一条静音规则。

        text 非空  → 语义静音：此后与这句话相似的消息都算「此类」，不再提醒；
        sender 非空 → 只静音这个来源里的这个人；
        两者都空   → 整个来源静音。
        已存在的待办一并转成 muted，列表和提醒里立刻消失；撤销后能恢复。
        返回 {'rule_id', 'created', 'muted_todos'}。
        """
        session = (session or "").strip()
        sender = (sender or "").strip()
        text = " ".join((text or "").split())[:200]
        if not session and not sender and not text:
            raise ValueError("session、sender、text 不能同时为空")
        vec_json = ""
        if text:
            vec_json = json.dumps(LocalEmbedder().embed([text])[0])
        created_at = datetime.now().isoformat(timespec="seconds")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO mutes (session, sender, text, vector, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (session, sender, text, vec_json, created_at))
        created = cur.rowcount > 0
        rule_id = self.conn.execute(
            "SELECT id FROM mutes WHERE session=? AND sender=? AND text=?",
            (session, sender, text)).fetchone()["id"]
        muted = self._apply_mute_rules()
        self.conn.commit()
        return {"rule_id": rule_id, "created": created, "muted_todos": muted}

    def _apply_mute_rules(self) -> int:
        """按当前全部规则，把还活着的待办里命中的那些一次性收成 muted。"""
        rules = self.muted_rules()
        seeds = self.semantic_seeds()
        if not rules and not seeds:
            return 0
        rows = self.conn.execute(
            "SELECT id, text, session, sender FROM todos "
            "WHERE status IN ('pending', 'unconfirmed')").fetchall()
        hit = [r["id"] for r in rows
               if self.is_muted(r["session"], r["sender"], rules)
               or self.matches_seed(r["text"], seeds)[0]]
        if hit:
            self.conn.executemany(
                "UPDATE todos SET status='muted' WHERE id=?", [(i,) for i in hit])
        return len(hit)

    def list_mutes(self) -> List[dict]:
        """列出静音规则，附带每条规则当前压住多少条待办。"""
        rows = self.conn.execute("SELECT * FROM mutes ORDER BY id").fetchall()
        emb = LocalEmbedder()
        live = self.conn.execute(
            "SELECT text, session, sender FROM todos WHERE status='muted'").fetchall()
        out = []
        for r in rows:
            if r["text"]:
                own = [(r["text"], _load_vec(r["vector"]) or emb.embed([r["text"]])[0])]
                count = sum(1 for t in live if self.matches_seed(t["text"], own)[0])
                kind = "kind"
            else:
                scope = [(r["session"], r["sender"])]
                count = sum(
                    1 for t in live if self.is_muted(t["session"], t["sender"], scope))
                kind = "sender" if r["sender"] else "source"
            item = dict(r)
            item.pop("vector", None)
            item["muted_count"] = count
            item["kind"] = kind
            out.append(item)
        return out

    def unmute(self, rule_id: int) -> dict:
        """按规则编号撤销静音；被撤销规则覆盖的条目恢复成 pending。"""
        row = self.conn.execute(
            "SELECT * FROM mutes WHERE id=?", (rule_id,)).fetchone()
        if not row:
            return {"rule_id": rule_id, "restored": 0, "found": False}
        self.conn.execute("DELETE FROM mutes WHERE id=?", (rule_id,))
        restored = self._restore_uncovered()
        self.conn.commit()
        return {"rule_id": rule_id, "restored": restored, "found": True,
                "session": row["session"], "sender": row["sender"],
                "text": row["text"]}

    def release(self, session: str, sender: str = "", text: str = "") -> int:
        """撤销覆盖这条待办的所有规则（控制台/看板「恢复提醒」用）。

        返回恢复成待办的条数；没有命中规则返回 0。
        """
        rows = self.conn.execute("SELECT * FROM mutes").fetchall()
        emb = LocalEmbedder()
        hit = []
        for r in rows:
            if r["text"]:
                if not text:
                    continue
                vec = _load_vec(r["vector"]) or emb.embed([r["text"]])[0]
                if self.matches_seed(text, [(r["text"], vec)])[0]:
                    hit.append(r["id"])
                continue
            if (not r["session"] or r["session"] == session) and (
                    not r["sender"] or r["sender"] == sender):
                hit.append(r["id"])
        if not hit:
            return 0
        self.conn.executemany("DELETE FROM mutes WHERE id=?", [(i,) for i in hit])
        restored = self._restore_uncovered()
        self.conn.commit()
        return restored

    def _restore_uncovered(self) -> int:
        """把已经不再被任何规则覆盖的 muted 条目恢复成 pending。"""
        rules = self.muted_rules()
        seeds = self.semantic_seeds()
        rows = self.conn.execute(
            "SELECT id, text, session, sender FROM todos WHERE status='muted'").fetchall()
        restore = [r["id"] for r in rows
                   if not self.is_muted(r["session"], r["sender"], rules)
                   and not self.matches_seed(r["text"], seeds)[0]]
        if restore:
            self.conn.executemany(
                "UPDATE todos SET status='pending' WHERE id=?", [(i,) for i in restore])
        return len(restore)

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
                "SELECT chat_id FROM messages WHERE ts=? "
                "AND (sender_name=? OR sender_id=?) LIMIT 1",
                (row["ts"], row["sender"], row["sender"])).fetchone()
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

    def calibration_insights(self) -> dict:
        """比 calibration_stats() 更详细的可观测数据：

        返回：
            stats:          calibration_stats() 原始计数
            active_groups:  有 >= 3 样本、已产生权重的 (chat_id, sender) 组数
            top_tags:       权重绝对值最大的 5 个 reason_tag，[(tag, adj), ...]
        """
        from collections import defaultdict

        stats = self.calibration_stats()
        weights_map = self.load_feedback_weights()
        # 所有权重拉平到 [(tag, adj)]，跨组取绝对值最大的
        tag_adj: dict[str, float] = defaultdict(float)
        for group_tags in weights_map.values():
            for tag, adj in group_tags.items():
                # 同 tag 在不同组可能都有，取绝对值最大的那次
                if abs(adj) > abs(tag_adj[tag]):
                    tag_adj[tag] = adj
        top_tags = sorted(tag_adj.items(), key=lambda kv: -abs(kv[1]))[:5]
        return {
            "stats": stats,
            "active_groups": len(weights_map),
            "top_tags": top_tags,
        }

    def explain_todo(self, todo_id: int, threshold: Optional[int] = None) -> Optional[dict]:
        """返回某条待办的完整判定报告（dict），CLI 与 dashboard popup 共用。

        结构：
            todo:        todos 表行（dict）
            tags:        [{tag, description, base_score, applied, adj, adj_score}]
            subtotal:    规则总分（不含校准）
            calib_delta: 用户校准带来的额外分（adj*7 累加）
            final_score: subtotal + calib_delta
            threshold:   判为 pending 的阈值（TODO_THRESHOLD=4）
            nearby:      原消息前后各 2 条（list[dict]）
            weights_hit: (chat_id, sender) 这组在 weights_map 里的条目
        """
        import chat2todo.core.extract as _ex

        row = self.conn.execute(
            "SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        if not row:
            return None

        # chat_id 从 messages 反查
        chat_id = ""
        try:
            mrow = self.conn.execute(
                "SELECT chat_id FROM messages WHERE ts=? "
                "AND (sender_name=? OR sender_id=?) LIMIT 1",
                (row["ts"], row["sender"], row["sender"])).fetchone()
            if mrow:
                chat_id = mrow["chat_id"] or ""
        except Exception:  # noqa: BLE001
            pass

        sender = row["sender"] or ""
        # 原消息内容
        original_msg = None
        try:
            mrow = self.conn.execute(
                "SELECT id, ts, sender_name, content, chat_id "
                "FROM messages WHERE ts=? AND (sender_name=? OR sender_id=?) LIMIT 1",
                (row["ts"], sender, sender)).fetchone()
            if mrow:
                original_msg = dict(mrow)
        except Exception:  # noqa: BLE001
            pass

        # 权重命中
        weights_map = self.load_feedback_weights()
        local_weights = weights_map.get((chat_id, sender), {})

        # reason 字段分号拆分（注意 "★重点 · " 前缀单独切出来）
        raw_reason = row["reason"] or ""
        display_star = raw_reason.startswith("★重点")
        clean_reason = raw_reason.replace("★重点 · ", "").replace("★重点", "").strip()
        tag_strs = [t.strip() for t in clean_reason.split(";") if t.strip()]

        subtotal = 0
        tag_items = []
        for tag in tag_strs:
            desc, base = _ex.tag_meta(tag)
            subtotal += base
            # extract.py score_message L143: if key and key in tag
            adj = 0.0
            for k, v in local_weights.items():
                if k and k in tag:
                    adj = v
                    break
            tag_items.append({
                "tag": tag,
                "description": desc,
                "base_score": base,
                "adj": adj,                 # 用户校准调整 [-0.5, 0.5]
                "adj_score": round(adj * 7.0, 2),   # 实际加到 score 上的量
            })

        calib_delta = round(sum(t["adj_score"] for t in tag_items), 2)
        final_score = round(subtotal + calib_delta, 2)

        # 附近消息（给用户看上下文）
        nearby = self.context(original_msg["id"]) if original_msg else []

        return {
            "todo": dict(row),
            "star": display_star,
            "tags": tag_items,
            "subtotal": subtotal,
            "calib_delta": calib_delta,
            "final_score": final_score,
            "threshold": threshold if threshold is not None else _ex.TODO_THRESHOLD,
            "confidence": row["confidence"],
            "status": row["status"],
            "chat_id": chat_id,
            "sender": sender,
            "original_msg": original_msg,
            "nearby": nearby,
            "weights_hit": local_weights,
        }

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
            # 同 todo_id + action 不重复（todo_id 保留原值，孤儿行由构造函数统一解绑）
            if self.conn.execute(
                    "SELECT id FROM feedback WHERE todo_id IS ? AND user_action=?",
                    (row["todo_id"], row["user_action"])).fetchone():
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
        self._heal_feedback_orphans()          # 待办没了，对应校准样本解绑
        self.conn.commit()
        return len(rows)

    def trash_item(self, todo_id: int) -> Optional[int]:
        """手动把一条待办丢进回收站，返回新回收站记录 id（撤销要用）。

        找不到这条待办时返回 None；调用方按真值判断即可，和旧版返回 bool 兼容。
        """
        r = self.conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        if not r:
            return None
        cur = self.conn.execute(
            "INSERT INTO trash (text, session, sender, ts, reason, confidence, "
            "status, created_at, done_at, deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["text"], r["session"], r["sender"], r["ts"], r["reason"],
             r["confidence"], r["status"], r["created_at"],
             r["done_at"] if "done_at" in r.keys() else "", self._today()),
        )
        self.conn.execute("DELETE FROM todos WHERE id=?", (todo_id,))
        self._heal_feedback_orphans()          # 待办没了，对应校准样本解绑
        self.conn.commit()
        return int(cur.lastrowid) if cur.lastrowid else None

    def list_trash(self, limit: int = 200) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trash ORDER BY deleted_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def count_trash(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM trash").fetchone()[0])

    def restore(self, trash_id: int) -> bool:
        """从回收站还原成待办（状态沿用删除前的状态）。

        同一条待办已存在（text/ts/sender 撞 UNIQUE）时 INSERT OR IGNORE 会静默丢弃，
        此时必须保留回收站记录并返回 False，否则待办彻底消失且界面还报成功。
        """
        r = self.conn.execute("SELECT * FROM trash WHERE id=?", (trash_id,)).fetchone()
        if not r:
            return False
        # 回收站里存了删除前的状态：已完成的不该还原成待办再提醒一遍
        status = (r["status"] or "pending").strip().lower()
        if status not in ("pending", "done", "muted"):
            status = "pending"
        done_at = (r["done_at"] or "") if status == "done" else ""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO todos (text, session, sender, ts, reason, "
            "confidence, status, created_at, done_at, reviewed) "
            "VALUES (?,?,?,?,?,?,?,?,?,1)",
            (r["text"], r["session"], r["sender"], r["ts"], r["reason"],
             r["confidence"], status, r["created_at"], done_at),
        )
        if not cur.rowcount:
            return False        # 已有同一条待办，回收站记录不动
        self.conn.execute("DELETE FROM trash WHERE id=?", (trash_id,))
        self.conn.commit()
        return True

    def purge_trash(self, days: int = 7) -> int:
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

    def checkpoint(self) -> None:
        """WAL 并回主库；一键清空前必须调用。"""
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def vacuum(self) -> None:
        """重写库文件，擦掉删除后残留的空闲页。"""
        try:
            self.conn.execute("VACUUM")
        except sqlite3.Error:
            pass

    def stats(self, since_ts: Optional[int] = None) -> dict:
        """统计。since_ts 给定时只统计该时间点之后的数据（c2t stats --since 7d）。"""
        if since_ts is None:
            return {
                "messages": self._count("messages"),
                "todos": self._count("todos"),
                "pending": self.conn.execute(
                    "SELECT COUNT(*) FROM todos WHERE status='pending'"
                ).fetchone()[0],
                "unconfirmed": self.conn.execute(
                    "SELECT COUNT(*) FROM todos WHERE status='unconfirmed'"
                ).fetchone()[0],
                "rejected": self.conn.execute(
                    "SELECT COUNT(*) FROM todos WHERE status='noise'"
                ).fetchone()[0],
                "done": self.conn.execute(
                    "SELECT COUNT(*) FROM todos WHERE status='done'"
                ).fetchone()[0],
                "muted": self.conn.execute(
                    "SELECT COUNT(*) FROM todos WHERE status='muted'"
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
            "unconfirmed": self.conn.execute(
                "SELECT COUNT(*) FROM todos WHERE status='unconfirmed' AND ts >= ?",
                (since_ts,),
            ).fetchone()[0],
            "rejected": self.conn.execute(
                "SELECT COUNT(*) FROM todos WHERE status='noise' AND ts >= ?", (since_ts,)
            ).fetchone()[0],
            "done": self.conn.execute(
                "SELECT COUNT(*) FROM todos WHERE status='done' AND ts >= ?", (since_ts,)
            ).fetchone()[0],
            "muted": self.conn.execute(
                "SELECT COUNT(*) FROM todos WHERE status='muted' AND ts >= ?", (since_ts,)
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
