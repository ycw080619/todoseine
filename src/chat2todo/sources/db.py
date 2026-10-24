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
        except Exception:  # noqa: BLE001 - 探测失败就退回默认位置
            pass
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
def _rows_to_messages(rows: Iterable[Dict]) -> List[Message]:
    """把后端吐出的行统一成 Message。字段名做宽松兼容。"""
    out: List[Message] = []
    for row in rows:
        text = row.get("content") or row.get("text") or ""
        ts = row.get("timestamp") or row.get("ts") or 0
        sender = row.get("sender") or row.get("sender_id") or ""
        name = row.get("sender_name") or row.get("name") or str(sender)
        if not text:
            continue
        out.append(
            Message(
                timestamp=int(ts),
                sender_id=str(sender),
                sender_name=str(name),
                content=str(text),
                type=int(row.get("type", 0)),
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
        return bool(self.module) and os.path.exists(self.module)

    def fetch(self, db: DBInfo, limit: int = 0) -> List[Dict]:
        if not self.available():
            raise C2TError("E4004", "社区解密模块未配置或不存在")
        cmd = [self.module]
        if self.extra_args:
            cmd += self.extra_args.split()
        cmd.append(db.path)
        if limit:
            cmd += ["--limit", str(limit)]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
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


@dataclass
class BuiltinBackend:
    """自建读取器骨架：标准 SQLCipher 页面布局，需要用户提供 key。"""

    key: str = ""
    key_file: str = ""

    name = "builtin"

    def available(self) -> bool:
        return bool(self._key())

    def _key(self) -> str:
        if self.key:
            return self.key
        if self.key_file and os.path.exists(self.key_file):
            return Path(self.key_file).read_text(encoding="utf-8").strip()
        return ""

    def fetch(self, db: DBInfo, limit: int = 0) -> List[Dict]:
        if not self.available():
            raise C2TError("E4002", "内置读取器需要 key（配置 [db].key 或 key_file）")
        from ..core.crypto import verify_sqlcipher_key

        if not verify_sqlcipher_key(self._key(), db.path):
            raise C2TError("E4003", "密钥校验失败：无法解开首页，密钥或 KDF 参数不对")
        raise C2TError(
            "E4003",
            "密钥有效，但内置读取器尚未实现整库抽取；请配置 [db].module 用社区工具导入",
        )


def get_backend(cfg: Dict) -> object:
    """按配置挑一个可用后端；都没有就抛可读错误。"""
    db = cfg.get("db", {}) if isinstance(cfg, dict) else {}
    community = CommunityBackend(module=db.get("module", ""), extra_args=db.get("args", ""))
    builtin = BuiltinBackend(key=db.get("key", ""), key_file=db.get("key_file", ""))
    if community.available():
        return community
    if builtin.available():
        return builtin
    raise C2TError(
        "E4002",
        "未配置可用的数据库后端：请在配置里设置 [db].module（社区解密工具路径）",
    )


def load_messages(cfg: Dict, limit: int = 0) -> List[Message]:
    db_cfg = cfg.get("db", {}) if isinstance(cfg, dict) else {}
    path = locate(db_cfg.get("path") or None)
    if not path:
        raise C2TError("E4001", "未找到 nt_msg.db：请用 [db].path 指定，或改用导出文件")
    info = inspect(str(path))
    rows = get_backend(cfg).fetch(info, limit=limit)  # type: ignore[attr-defined]
    return _rows_to_messages(rows)
