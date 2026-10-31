"""消息 Markdown 归档（按 群名/年/月/日.md）+ 去重清单 manifest.json。

文档要求：原始消息按「群名/年/月」分目录、按日成文，并附带一份清单，
二次启动或重跑同步时不重复追加。这里的实现只做纯文件 IO，不碰数据库。

去重清单是按日文件维度的，键是「时间 + 发送者 + 内容哈希」，
和 Store 的去重键口径一致；同一句话发到两个群会分别落进两个群的文件，
不会互相吞掉。

隐私：归档路径下默认脱敏（白名单群除外），这与「AI 请求强制脱敏」
是两条独立开关，都在 core/privacy.py 里统一实现。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Iterable, List

from ..models import Message

log = logging.getLogger("todoseine.archive")

MANIFEST_NAME = "manifest.json"
_LOCK = threading.Lock()          # 同一进程内串行写，避免 manifest 写坏

_BAD_CHARS = '<>:"/\\|?*'


def _safe_name(name: str) -> str:
    """群名直接当目录名：去掉 Windows 非法字符，并防住 .. 之类的穿越。"""
    text = "".join("_" if ch in _BAD_CHARS else ch for ch in (name or "未命名"))
    text = text.strip().strip(".")
    return text or "未命名"


def default_archive_dir(db_path: str) -> str:
    if not db_path:
        return ""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "archive")


def _manifest_path(base_dir: str) -> str:
    return os.path.join(base_dir, MANIFEST_NAME)


def _load_manifest(base_dir: str) -> Dict[str, Any]:
    path = _manifest_path(base_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "files": {}}


def _save_manifest(base_dir: str, data: Dict[str, Any]) -> None:
    path = _manifest_path(base_dir)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)       # 原子替换，写一半断电也不会毁掉清单
    except OSError as exc:
        log.warning("写 manifest 失败：%s", exc)


def _msg_uid(msg: Message) -> str:
    raw = f"{msg.timestamp}|{msg.sender_id}|{len(msg.content or '')}|{msg.content_hash}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _day_parts(ts: int) -> tuple:
    local = time.localtime(ts or time.time())
    return str(local.tm_year), f"{local.tm_mon:02d}", time.strftime("%Y-%m-%d", local), \
        time.strftime("%H:%M:%S", local)


def _render_line(msg: Message, text: str, clock: str) -> str:
    sender = (msg.sender_name or msg.sender_id or "未知").replace("\n", " ")
    body = (text or "").replace("\r\n", "\n").strip()
    body = body.replace("\n", "\n  ")      # 多行内容缩进两格，保持列表结构
    return f"- {clock} **{sender}**：{body}"


def _file_header(session: str, day: str) -> str:
    return f"# {session} · {day}\n\n> 由 TodoSeine 自动归档，仅追加不覆盖。\n\n"


class Archiver:
    """写 Markdown 归档；所有方法都吞异常，绝不影响采集主流程。"""

    def __init__(self, cfg: Dict[str, Any], db_path: str = ""):
        archive = dict(cfg.get("archive") or {})
        self.enabled = archive.get("enabled", True) is not False
        self.base_dir = str(archive.get("dir") or "") or default_archive_dir(
            db_path or str(cfg.get("db_path") or ""))
        self._cfg = cfg

    def write(self, messages: Iterable[Message], session: str = "",
              chat_id: str = "") -> int:
        """追加一批消息，返回真正新写的条数。"""
        msgs = list(messages)
        if not self.enabled or not self.base_dir or not msgs:
            return 0
        from .privacy import redact_for_archive

        written = 0
        try:
            with _LOCK:
                manifest = _load_manifest(self.base_dir)
                files = manifest["files"]
                for msg in msgs:
                    group = session or msg.chat_id or "未分类"
                    year, month, day, clock = _day_parts(msg.timestamp)
                    rel = os.path.join(_safe_name(group), year, month, f"{day}.md")
                    key = rel.replace("\\", "/")
                    entry = files.setdefault(key, {"messages": []})
                    uid = _msg_uid(msg)
                    if uid in entry["messages"]:
                        continue
                    text, _hits = redact_for_archive(
                        msg.content or "", group, self._cfg)
                    path = os.path.join(self.base_dir, rel)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    new_file = not os.path.exists(path)
                    with open(path, "a", encoding="utf-8") as fh:
                        if new_file:
                            fh.write(_file_header(group, day))
                        fh.write(_render_line(msg, text, clock) + "\n")
                    entry["messages"].append(uid)
                    written += 1
                if written:
                    _save_manifest(self.base_dir, manifest)
        except Exception as exc:  # noqa: BLE001 - 归档失败不能拖垮采集
            log.warning("Markdown 归档失败：%s", exc)
            return 0
        return written


def archive_messages(cfg: Dict[str, Any], messages: List[Message],
                     session: str = "", db_path: str = "") -> int:
    """一次性归档入口（CLI / 监听线程都走这里）。"""
    return Archiver(cfg, db_path).write(messages, session=session)
