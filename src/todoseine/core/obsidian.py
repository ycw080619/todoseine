"""Obsidian 双向同步（实验特性，默认本地 → Obsidian 单向镜像）。

文档口径（这里逐条落地）：
  * 待办写成标准 ``- [ ]`` 任务行，兼容 Tasks / Dataview；
  * 每行带一个隐藏 UUID（``%%c2t:<uuid>%%``），群名 / 笔记改名后仍能回写；
  * 写文件加锁，基于 MD5 做增量判断，没变化就不动磁盘；
  * 两侧同时修改时按 mtime 判优先级，冲突内容另存 ``.conflict.bak`` 并提示；
  * 在 Obsidian 勾选完成 → 回写 SQLite；在 Obsidian 删除任务 → 只删镜像；
  * 关闭同步不会删除任何历史归档。

同步范围只碰 ``<vault>/<子目录>`` 下由本程序生成的两个文件：
``待办.md`` 与 ``待办.conflict.bak``，不扫描、不改动库里其他笔记。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ..models import TodoItem

log = logging.getLogger("todoseine.obsidian")

SUBDIR = "Chat2Todo"
TODO_NOTE = "待办.md"
STATE_NAME = ".c2t_sync.json"
LOCK_NAME = ".c2t.lock"

_UUID_OPEN = "%%c2t:"
_UUID_CLOSE = "%%"
_LOCK_STALE_SEC = 120


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _lock_path(folder: str) -> str:
    return os.path.join(folder, LOCK_NAME)


def acquire_lock(folder: str, wait: float = 2.0) -> bool:
    """跨进程文件锁；超过 120 秒的锁视为残留（上次崩溃留下）直接接管。"""
    path = _lock_path(folder)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        return False
    deadline = time.time() + wait
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > _LOCK_STALE_SEC:
                    os.remove(path)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                return False
            time.sleep(0.05)
        except OSError:
            return False


def release_lock(folder: str) -> None:
    try:
        os.remove(_lock_path(folder))
    except OSError:
        pass


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _task_line(todo: TodoItem, uid: str) -> str:
    box = "x" if todo.status == "done" else " "
    text = (todo.text or "").replace("\n", " ").strip() or "（空）"
    meta = f" %%c2t:{uid}%%"
    due = self_due_label(todo)
    if due:
        meta += f" 📅 {due}"
    return f"- [{box}] {text}{meta}"


def self_due_label(todo: TodoItem) -> str:
    """从待办文本里解析到期日，写成 Tasks 插件认的 📅 标记。"""
    from .deadline import parse as parse_due

    try:
        due = parse_due(todo.text or "")
    except Exception:  # noqa: BLE001
        return ""
    if not due:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(due))


def _parse_line(line: str) -> Optional[Tuple[str, bool, str]]:
    """解析一行任务，返回 (uuid, 是否完成, 文本)；不是带 UUID 的任务返回 None。"""
    stripped = line.strip()
    if not stripped.startswith(("- [ ]", "- [x]", "- [X]")):
        return None
    done = stripped[3] in ("x", "X")
    start = stripped.find(_UUID_OPEN)
    if start < 0:
        return None
    end = stripped.find(_UUID_CLOSE, start + len(_UUID_OPEN))
    if end < 0:
        return None
    uid = stripped[start + len(_UUID_OPEN): end].strip()
    if not uid:
        return None
    body = stripped[5:start].strip()
    if body.startswith("📅"):
        body = ""
    return uid, done, body


class ObsidianSync:
    def __init__(self, vault: str, store, cfg: Optional[Dict[str, Any]] = None):
        self.vault = os.path.abspath(vault) if vault else ""
        self.store = store
        self.cfg = cfg or {}
        self.folder = os.path.join(self.vault, SUBDIR) if self.vault else ""
        self.note = os.path.join(self.folder, TODO_NOTE) if self.folder else ""
        self.state_path = os.path.join(self.folder, STATE_NAME) if self.folder else ""
        self._state: Dict[str, Any] = {}

    # ---------- 状态 ----------

    @property
    def usable(self) -> bool:
        return bool(self.vault and os.path.isdir(self.vault))

    def _load_state(self) -> Dict[str, Any]:
        if self._state:
            return self._state
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._state = data
        except (OSError, ValueError):
            self._state = {}
        self._state.setdefault("uuids", {})       # todo_id -> uuid
        self._state.setdefault("note_md5", "")
        self._state.setdefault("note_mtime", 0.0)
        self._state.setdefault("db_digest", "")
        return self._state

    def _save_state(self) -> None:
        if not self.folder:
            return
        try:
            os.makedirs(self.folder, exist_ok=True)
            _atomic_write(self.state_path, json.dumps(self._state, ensure_ascii=False, indent=1))
        except OSError as exc:
            log.warning("写 Obsidian 同步状态失败：%s", exc)

    # ---------- 推送 ----------

    def _render(self, todos: List[TodoItem]) -> Tuple[str, str]:
        state = self._load_state()
        uuids: Dict[str, str] = state["uuids"]
        existing = {v: k for k, v in uuids.items()}
        lines = [
            "# TodoSeine 待办",
            "",
            "> 由 TodoSeine 自动生成，请勿手改 UUID 注释；勾选完成会回写本地数据库。",
            "> 在 Obsidian 删除任务只删除镜像，不会删除本地事实源。",
            "",
        ]
        for todo in todos:
            uid = uuids.get(str(todo.todo_id))
            if not uid:
                uid = existing.get(str(todo.todo_id)) or uuid.uuid4().hex
                uuids[str(todo.todo_id)] = uid
            lines.append(_task_line(todo, uid))
        # 摘要必须按「落到磁盘的完整内容」算，否则末尾换行会让每次 push 都判定为变化
        text = "\n".join(lines) + "\n"
        return text, _md5(text)

    def push(self, force: bool = False) -> bool:
        """把本地待办镜像到 Obsidian；内容没变就完全不动磁盘。"""
        if not self.usable or not self.folder:
            return False
        try:
            os.makedirs(self.folder, exist_ok=True)
        except OSError as exc:
            log.warning("创建 Obsidian 同步目录失败：%s", exc)
            return False
        if not acquire_lock(self.folder):
            log.info("Obsidian 同步目录被占用，本次跳过")
            return False
        try:
            return self._push_locked(force)
        finally:
            release_lock(self.folder)

    def _push_locked(self, force: bool = False) -> bool:
        """调用方必须已经持有目录锁；pull 回写后就复用这条路，避免重入死锁。"""
        try:
            state = self._load_state()
            todos = self.store.list_todos(None)
            text, digest = self._render(todos)
            old_md5 = state.get("note_md5") or ""
            exists = os.path.isfile(self.note)
            disk_md5 = ""
            if exists:
                try:
                    with open(self.note, "r", encoding="utf-8") as fh:
                        disk_md5 = _md5(fh.read())
                except OSError:
                    disk_md5 = ""
            # 用户改过笔记（磁盘与上次记录不一致，且与本地库也不一致）→ 备份冲突
            if exists and disk_md5 and old_md5 and disk_md5 != old_md5 and disk_md5 != digest:
                self._backup_conflict(disk_md5)
            if disk_md5 == digest and exists:
                state["note_md5"] = digest
                state["note_mtime"] = os.path.getmtime(self.note)
                self._save_state()
                return False
            _atomic_write(self.note, text)
            state["note_md5"] = digest
            state["note_mtime"] = os.path.getmtime(self.note)
            state["db_digest"] = digest
            self._save_state()
            log.info("Obsidian 镜像已更新：%d 条待办", len(todos))
            return True
        except OSError as exc:
            log.warning("写 Obsidian 笔记失败：%s", exc)
            return False

    def _backup_conflict(self, disk_md5: str) -> None:
        """两侧同时改过：把用户版本另存 .conflict.bak，再继续镜像。"""
        backup = os.path.join(self.folder, "待办.conflict.bak")
        try:
            with open(self.note, "r", encoding="utf-8") as src, \
                    open(backup, "w", encoding="utf-8", newline="\n") as dst:
                dst.write(src.read())
            log.warning("Obsidian 待办笔记被外部修改，已备份到 %s", backup)
        except OSError as exc:
            log.warning("冲突备份失败：%s", exc)

    # ---------- 拉取（完成回写 / 删除只删镜像） ----------

    def pull(self) -> Dict[str, int]:
        """把 Obsidian 里的勾选状态回写本地；删除的行只从镜像消失。"""
        result = {"done": 0, "conflict": 0, "missing": 0}
        if not self.usable or not os.path.isfile(self.note):
            return result
        if not acquire_lock(self.folder):
            return result
        try:
            with open(self.note, "r", encoding="utf-8") as fh:
                text = fh.read()
            state = self._load_state()
            disk_md5 = _md5(text)
            if disk_md5 == state.get("note_md5"):
                return result              # 没动过，不用回写
            by_uuid = {v: int(k) for k, v in state["uuids"].items()}
            seen = set()
            changed = False
            for line in text.splitlines():
                parsed = _parse_line(line)
                if not parsed:
                    continue
                uid, done, _body = parsed
                seen.add(uid)
                todo_id = by_uuid.get(uid)
                if todo_id is None:
                    continue
                if done:
                    if self.store.set_status(todo_id, "done"):
                        result["done"] += 1
                        changed = True
            missing = [uid for uid in by_uuid if uid not in seen]
            result["missing"] = len(missing)
            state["note_md5"] = disk_md5
            state["note_mtime"] = os.path.getmtime(self.note)
            self._save_state()
            if changed:
                # 回写成功后立刻重新渲染一次，让勾选状态与库保持一致。
                # 这里必须走锁内的 _push_locked，重复 acquire_lock 会自我阻塞。
                self._push_locked(force=True)
            return result
        except OSError as exc:
            log.warning("读 Obsidian 笔记失败：%s", exc)
            return result
        finally:
            release_lock(self.folder)

    def sync(self) -> Dict[str, int]:
        """先拉取（回写完成态）再推送（补新待办），返回统计。"""
        pulled = self.pull()
        self.push()
        return pulled


def make_sync(cfg: Dict[str, Any], store) -> Optional[ObsidianSync]:
    """按配置建同步器；未配置库路径时返回 None（功能整体关闭）。"""
    vault = str(cfg.get("obsidian_vault") or "")
    if not vault:
        return None
    sync = ObsidianSync(vault, store, cfg)
    return sync if sync.usable else None
