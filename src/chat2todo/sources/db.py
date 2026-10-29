"""数据库只读通道（可选，默认关闭）。

两条实现路径同时存在：
1. **社区后端**（首选）：在配置里指定一个已经打通解密的第三方工具，
   由本项目以子进程方式调用，读取其输出的 JSONL；
2. **内置后端**（自建骨架）：标准 SQLCipher 布局的读取器，
   需要用户自己提供 key；腾讯自研的 WCDB 变体目前不在内置范围内。

设计原则：
- 只读：任何情况下都不写 QQ 目录、不动原库文件；
- 可降级：后端不可用时抛 C2TError，提示改用导出文件，而不是崩；
- 零第三方依赖：调用社区工具走 subprocess，不 import 它的任何库。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..errors import C2TError
from ..models import Message

HEADER_SIZE = 1024
PAGE_SIZE = 4096
MAGIC = b"SQLite header 3"
SQLITE_MAGIC = b"SQLite format 3\x00"


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------
def _documents_dirs() -> List[Path]:
    """拿「文档」目录的真实位置。

    Windows 上「文档」常被重定向到别的盘，直接拼 ~/Documents 会找不到；
    这里优先读注册表里的已知文件夹，读不到再退回默认猜测。
    """
    dirs: List[Path] = []
    if os.name == "nt":
        try:
            import winreg  # 标准库

            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as hk:
                for name in ("Personal", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"):
                    try:
                        val, _ = winreg.QueryValueEx(hk, name)
                    except OSError:
                        continue
                    expanded = os.path.expandvars(str(val))
                    if os.path.isdir(expanded):
                        dirs.append(Path(expanded))
        except Exception as exc:  # noqa: BLE001 - 探测失败就退回默认位置
            logging.getLogger("chat2todo.sources.db").debug(
                "读取注册表 Documents 位置失败，退回默认目录：%s", exc)
    home_docs = Path.home() / "Documents"
    if home_docs.is_dir():
        dirs.append(home_docs)
    return dirs


def candidate_roots() -> List[Path]:
    """常见的 QQ NT 数据根目录（用户目录可能被重定向，所以枚举已知位置）。"""
    roots = []
    for docs in _documents_dirs():
        p = docs / "Tencent Files"
        if p.exists():
            roots.append(p)
    seen, out = set(), []
    for r in roots:
        s = str(r).lower()
        if s not in seen:
            seen.add(s)
            out.append(r)
    return out


def locate(root: Optional[str] = None) -> Optional[Path]:
    """找到 nt_msg.db；找不到返回 None。只做查找，不打开。"""
    if root:
        p = Path(root)
        if p.is_file():
            return p
        for hit in p.glob("*/nt_qq/nt_db/nt_msg.db"):
            return hit
        return None
    for base in candidate_roots():
        for hit in base.glob("*/nt_qq/nt_db/nt_msg.db"):
            return hit
    return None


@dataclass
class DBInfo:
    path: str
    size: int
    encrypted: bool
    pages: int
    header: bytes

    @property
    def readable_by_sqlite(self) -> bool:
        return self.header.startswith(SQLITE_MAGIC)


def inspect(path: str) -> DBInfo:
    p = Path(path)
    if not p.exists():
        raise C2TError("E4001", f"未找到数据库：{path}")
    size = p.stat().st_size
    with open(p, "rb") as fh:
        head = fh.read(HEADER_SIZE + 64)
    encrypted = head.startswith(MAGIC) and not head.startswith(SQLITE_MAGIC)
    body = max(0, size - HEADER_SIZE)
    pages = body // PAGE_SIZE
    return DBInfo(str(p), size, encrypted, pages, head[:64])


# --------------------------------------------------------------------------
# 后端
# --------------------------------------------------------------------------
def _rows_to_messages(rows: Iterable[Dict], star_groups: set = frozenset(),
                      star_senders: set = frozenset()) -> List[Message]:
    """把后端吐出的行统一成 Message。字段名做宽松兼容。"""
    out: List[Message] = []
    for row in rows:
        text = row.get("content") or row.get("text") or ""
        ts = row.get("timestamp") or row.get("ts") or 0
        # 真正的发言者 ID 字段名各家不一，先按常见别名找；
        # 找不到时才退回 sender / sender_id（有些导出工具把群号塞在这里）
        uid = (row.get("sender_uid") or row.get("sender_uin") or row.get("uin")
               or row.get("uid") or row.get("user_id") or "")
        sender = uid or row.get("sender") or row.get("sender_id") or ""
        name = row.get("sender_name") or row.get("name") or str(sender)
        if not text:
            continue
        chat = str(row.get("chat_id") or "")
        sender_s = str(sender)
        if uid == "" and chat and sender_s and sender_s == chat and name and name != chat:
            # sender_id 和 chat_id 一模一样 → 这个字段其实是群号。
            # 用昵称作替代键，至少不会让「同群不同人」在去重时互相吞掉。
            sender_s = str(name)
        starred = bool(
            (chat and chat in star_groups)
            or (sender_s and sender_s in star_groups)
            or (sender_s and sender_s in star_senders)
        )
        out.append(
            Message(
                timestamp=int(ts),
                sender_id=sender_s,
                sender_name=str(name),
                content=str(text),
                type=int(row.get("type", 0)),
                star=starred,
                chat_id=chat,
            )
        )
    return out


@dataclass
class CommunityBackend:
    """调用外部解密工具（如 nt_msg_db_util 一类），读它的 JSONL 输出。"""

    module: str = ""
    extra_args: str = ""

    name = "community"

    def available(self) -> bool:
        # 必须是可执行的文件；os.path.exists 对目录也返回 True，
        # 配置里填个目录就会把子进程启动成「执行目录」，静默失败或更糟。
        return bool(self.module) and os.path.isfile(self.module)

    def fetch(self, db: DBInfo, limit: int = 0, since: int = 0) -> List[Dict]:
        if not self.available():
            raise C2TError("E4004", "社区解密模块未配置或不存在")
        cmd = [self.module]
        if self.extra_args:
            cmd += self.extra_args.split()
        cmd.append(db.path)
        if limit:
            cmd += ["--limit", str(limit)]
        # 子进程不该看到数据库口令：把它排除掉，避免被第三方工具顺手记录
        env = {k: v for k, v in os.environ.items() if k != "C2T_DB_KEY"}
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=300, env=env)
        except OSError as exc:
            raise C2TError("E4004", f"无法启动社区模块：{exc}") from exc
        out = proc.stdout.decode("utf-8", "replace").strip()
        if proc.returncode != 0 or not out:
            raise C2TError("E4004", f"社区模块返回失败（code={proc.returncode}）")
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows


# --------------------------------------------------------------------------
# 内置后端：sqlcipher3 直读（可选依赖 pip install chat2todo[db]）
# --------------------------------------------------------------------------

#: 社区实测通过的 QQ NT 库 PRAGMA（key 之后执行，顺序不能变）
SQLCIPHER_PRAGMAS = (
    "PRAGMA kdf_iter = 4000",
    "PRAGMA cipher_hmac_algorithm = HMAC_SHA1",
    "PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512",
)

#: 页大小不是 SQLCipher 默认值，必须在 PRAGMA key 之前设置
PRAGMA_BEFORE_KEY = "PRAGMA cipher_page_size = 4096"

#: 消息表与列（数字列名是腾讯 WCDB 的列编码）
MSG_TABLES = ("group_msg_table", "c2c_msg_table")
# 40001=msgId 40033=会话号（群表=群号，私聊表=对方QQ）40021/40027=发送者
# 40050=时间戳 40093=昵称 40800=protobuf 正文；注意 40002 是 msgRandom，不是群号
MSG_COLUMNS = ('"40001"', '"40033"', '"40021"', '"40027"', '"40050"', '"40093"', '"40800"')


def _clean_sender(name: object, sender: str) -> str:
    """40093 有时不是昵称（纯数字或过短），这时退回 QQ 号。"""
    n = str(name or "").strip()
    return sender if (not n or n.isdigit() or len(n) <= 1) else n


def _decode_records(records: Iterable[tuple]) -> List[Dict]:
    """原始行 → 后端统一行格式；40800 用 pbbody 解 protobuf。"""
    from ..core.pbbody import decode_message_text

    rows: List[Dict] = []
    for msg_id, chat_id, s1, s2, ts, nick, body in records:
        content = decode_message_text(body or b"")
        if len(content) < 2 or len(content) > 400:
            continue
        sender = str(s2 or s1 or "")
        rows.append({
            "timestamp": int(ts or 0),
            "sender": sender,
            "sender_name": _clean_sender(nick, sender),
            "content": content,
            "type": 0,
            "chat_id": str(chat_id or ""),
            "msg_id": str(msg_id or ""),
        })
    return rows


def _snapshot_without_header(src: str, dst_dir: str) -> str:
    """活库不能直接读：先拍快照，再剥掉 1024 字节自定义头。"""
    import shutil

    snap = os.path.join(dst_dir, "snap.db")
    shutil.copy2(src, snap)
    stripped = os.path.join(dst_dir, "stripped.db")
    with open(snap, "rb") as fi, open(stripped, "wb") as fo:
        fi.seek(HEADER_SIZE)
        shutil.copyfileobj(fi, fo)
    os.remove(snap)
    return stripped


@dataclass
class BuiltinBackend:
    """内置读取器：sqlcipher3 引擎直读 QQ NT 库，全程只读、明文不落盘。"""

    key: str = ""
    key_file: str = ""

    name = "builtin"

    def _key(self) -> str:
        if self.key:
            return self.key
        if self.key_file and os.path.exists(self.key_file):
            return Path(self.key_file).read_text(encoding="utf-8").strip()
        return ""

    @staticmethod
    def engine_available() -> bool:
        try:
            import sqlcipher3  # noqa: F401
        except ImportError:
            return False
        return True

    def available(self) -> bool:
        return bool(self._key()) and self.engine_available()

    def fetch(self, db: DBInfo, limit: int = 0, since: int = 0) -> List[Dict]:
        key = self._key()
        if not key:
            raise C2TError("E4002", "没有密钥：先 `c2t db-key --save` 保存，或配置 [db].key")
        if not self.engine_available():
            raise C2TError("E4002", "缺少 sqlcipher3 引擎：pip install chat2todo[db]")

        import shutil
        import tempfile

        import sqlcipher3

        tmp = tempfile.mkdtemp(prefix="c2t_db_")
        try:
            stripped = _snapshot_without_header(db.path, tmp)
            conn = sqlcipher3.connect(stripped)
            try:
                conn.execute(PRAGMA_BEFORE_KEY)
                # PRAGMA key 紧随其后；口令里的单引号按 SQL 规则翻倍
                conn.execute(f"PRAGMA key = '{key.replace(chr(39), chr(39) * 2)}'")
                for pragma in SQLCIPHER_PRAGMAS:
                    conn.execute(pragma)
                records: List[tuple] = []
                for table in MSG_TABLES:
                    cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
                    if not {"40050", "40800"} <= cols:
                        continue
                    sql = (
                        f"SELECT {', '.join(MSG_COLUMNS)} FROM \"{table}\" "
                        'WHERE "40050" >= ? ORDER BY "40050" DESC'
                    )
                    params: list = [int(since)]
                    if limit:
                        sql += " LIMIT ?"
                        params.append(int(limit))
                    records.extend(conn.execute(sql, params))
            finally:
                conn.close()
        except C2TError:
            raise
        except Exception as exc:  # sqlcipher3 的 DatabaseError 等
            raise C2TError(
                "E4003", f"解密或读取失败：{exc}（多为密钥错误或 QQ 版本变更）"
            ) from exc
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return _decode_records(records)


def resolve_key(db_cfg: Dict) -> str:
    """密钥解析优先级：配置 key > 环境变量 > key_file > DPAPI 本机密库。"""
    key = db_cfg.get("key") or os.environ.get("C2T_DB_KEY", "")
    if key:
        return key
    key_file = db_cfg.get("key_file", "")
    if key_file and os.path.exists(key_file):
        return Path(key_file).read_text(encoding="utf-8").strip()
    from ..core import keybox

    return keybox.load_key()


def get_backend(cfg: Dict) -> object:
    """按配置挑一个可用后端；都没有就抛可读错误。"""
    db = cfg.get("db", {}) if isinstance(cfg, dict) else {}
    community = CommunityBackend(module=db.get("module", ""), extra_args=db.get("args", ""))
    if community.available():
        return community
    builtin = BuiltinBackend(key=resolve_key(db))
    if builtin.available():
        return builtin
    if not builtin.engine_available():
        raise C2TError("E4002", "数据库通道需要 sqlcipher3：pip install chat2todo[db]")
    raise C2TError(
        "E4002",
        "没有可用密钥：运行 `c2t db-key --save` 交互保存（DPAPI 加密），或配置 [db].key",
    )


def _id_set(value: object) -> set:
    """配置里的群号/QQ 号：支持列表，也支持逗号分隔字符串。"""
    if isinstance(value, str):
        items = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = []
    return {str(x).strip() for x in items if str(x).strip()}


def _apply_filters(rows: Iterable[Dict], cfg: Dict, keep_ignored: bool = False):
    """按 [filter] 做两件事：免打扰的来源整条丢弃；特别关心的来源打上 star。

    免打扰/特别关心都同时匹配「群号」与「发送者 QQ 号」——用户不关心这个号码
    是群还是人（如群机器人），只要不想看就丢掉。
    keep_ignored=True 时只打星标、不丢免打扰来源——回填历史来源群时要用，
    因为这些消息早就在库里了，需要知道它们属于哪个群才能清理。
    """
    conf = cfg.get("filter", {}) if isinstance(cfg, dict) else {}
    ignore = _id_set(conf.get("ignore_groups", []))
    star_groups = _id_set(conf.get("star_groups", []))
    star_senders = _id_set(conf.get("star_senders", []))
    if not (ignore or star_groups or star_senders):
        return list(rows), star_groups, star_senders

    kept: List[Dict] = []
    for row in rows:
        chat = str(row.get("chat_id") or "")
        sender = str(row.get("sender") or "")
        if not keep_ignored and ((chat and chat in ignore)
                                 or (sender and sender in ignore)):
            continue
        kept.append(row)
    return kept, star_groups, star_senders


def load_messages(cfg: Dict, limit: int = 0, days: int = 0,
                  keep_ignored: bool = False) -> List[Message]:
    import time

    db_cfg = cfg.get("db", {}) if isinstance(cfg, dict) else {}
    path = locate(db_cfg.get("path") or None)
    if not path:
        raise C2TError("E4001", "未找到 nt_msg.db：请用 [db].path 指定，或改用导出文件")
    info = inspect(str(path))
    since = int(time.time()) - days * 86400 if days > 0 else 0
    backend = get_backend(cfg)
    rows = backend.fetch(info, limit=limit, since=since)  # type: ignore[attr-defined]
    rows, star_groups, star_senders = _apply_filters(
        rows, cfg, keep_ignored=keep_ignored)
    return _rows_to_messages(rows, star_groups=star_groups, star_senders=star_senders)


#: 群名不在消息库里，在同级 group_info.db 的这张表
#: （60001=群号，66530=内部群代码=消息里的 chat_id，60007=群名）
GROUP_INFO_TABLE = "group_detail_info_ver1"


def fetch_group_names(cfg: Dict) -> Dict[str, str]:
    """读 nt_msg.db 同级的 group_info.db，返回 {群号或内部代码: 群名}。

    同一群有两个 ID：用户看到的群号（60001，控制台过滤名单里配的就是它）和
    消息库 chat_id 用的内部代码（66530）。两个键都写进映射，展示层拿到哪个
    都能查出群名。
    同一密钥、同一开库手法（快照 → 剥头 → SQLCipher PRAGMA 序列）。
    群名只是展示层的锦上添花：任何一步失败都返回空 dict，绝不抛异常，
    以免影响消息同步主流程。
    """
    import shutil
    import tempfile

    try:
        db_cfg = cfg.get("db", {}) if isinstance(cfg, dict) else {}
        path = locate(db_cfg.get("path") or None)
        if not path:
            return {}
        info_path = path.parent / "group_info.db"
        if not info_path.exists():
            return {}
        if not BuiltinBackend.engine_available():
            return {}
        key = resolve_key(db_cfg)
        if not key:
            return {}
        import sqlcipher3

        tmp = tempfile.mkdtemp(prefix="c2t_ginfo_")
        try:
            stripped = _snapshot_without_header(str(info_path), tmp)
            conn = sqlcipher3.connect(stripped)
            try:
                conn.execute(PRAGMA_BEFORE_KEY)
                conn.execute(f"PRAGMA key = '{key.replace(chr(39), chr(39) * 2)}'")
                for pragma in SQLCIPHER_PRAGMAS:
                    conn.execute(pragma)
                rows = conn.execute(
                    f'SELECT "60001", "66530", "60007" FROM "{GROUP_INFO_TABLE}"'
                ).fetchall()
            finally:
                conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        out: Dict[str, str] = {}
        for num, code, name in rows:
            name = str(name or "").strip()
            if not name:
                continue
            num, code = str(num or "").strip(), str(code or "").strip()
            if num:
                out[num] = name
            if code:
                out[code] = name
        return out
    except Exception as exc:  # noqa: BLE001 - 展示层辅助功能，降级为不显示群名
        logging.getLogger("chat2todo.sources.db").debug(
            "读取群名失败，看板将只显示群号：%s", exc)
        return {}
