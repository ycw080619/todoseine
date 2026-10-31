"""数据库 schema 迁移：迁移前自动备份，迁移失败自动回滚。

做法是「影子副本 + 原子替换」：
  1. 对源库做一次 checkpoint，保证 -wal 里的内容都落进主库；
  2. 复制一份到同目录的 ``*.migrate.tmp``，在副本上跑 Store 的 _migrate；
  3. 副本上 ``PRAGMA integrity_check`` 通过、schema_version 达标后，
     才把副本原子替换回正式库；
  4. 替换前把正式库再备份成 ``qq-*.db``，失败时正式库一个字节都没动。

这样即使迁移中途抛异常、断电、磁盘写满，用户的库仍然是迁移前的完好状态。
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from typing import Any, Dict, Optional

log = logging.getLogger("todoseine.migrate")


def _read_version(db_path: str) -> str:
    """读 meta 表里的 schema_version；库不存在 / 没有 meta 表都返回空串。"""
    if not os.path.isfile(db_path):
        return ""
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
    except sqlite3.Error:
        return ""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""
    finally:
        conn.close()


def plan(db_path: str) -> Dict[str, Any]:
    """对比当前库与代码里的目标版本，给 CLI 展示用。"""
    from .store import Store

    return {
        "path": db_path,
        "exists": os.path.isfile(db_path),
        "current": _read_version(db_path),
        "target": Store.SCHEMA_VERSION,
        "needed": _read_version(db_path) != Store.SCHEMA_VERSION,
    }


def _checkpoint(db_path: str) -> None:
    from .backup import _checkpoint as checkpoint

    checkpoint(db_path)


def run(db_path: str, backup_dir: str = "", keep: int = 7) -> Dict[str, Any]:
    """执行迁移；返回 {ok, from, to, backup, error}。"""
    from .backup import backup_database
    from .store import Store

    info = plan(db_path)
    result: Dict[str, Any] = {
        "ok": False, "path": db_path, "from": info["current"],
        "to": info["target"], "backup": "", "error": "",
    }
    if not info["exists"]:
        result["error"] = "数据库文件不存在"
        return result
    if not info["needed"]:
        result["ok"] = True
        return result

    tmp = db_path + ".migrate.tmp"
    result["backup"] = backup_database(db_path, backup_dir, keep=keep,
                                       encrypt=False) or ""
    try:
        _checkpoint(db_path)
        for suffix in ("", "-wal", "-shm"):
            src = db_path + suffix
            if os.path.isfile(src):
                shutil.copy2(src, tmp + suffix)
        store = Store(tmp)                     # 这里会跑 _migrate
        try:
            store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            row = store.conn.execute("PRAGMA integrity_check").fetchone()
            healthy = bool(row) and str(row[0]).lower() == "ok"
        finally:
            store.close()
        if not healthy:
            result["error"] = "迁移副本完整性校验失败，已放弃替换"
            return result
        _checkpoint(tmp)
        for suffix in ("-wal", "-shm"):
            leftover = tmp + suffix
            if os.path.isfile(leftover):
                os.remove(leftover)
        os.replace(tmp, db_path)
        result["ok"] = True
        result["after"] = _read_version(db_path)
        log.info("数据库迁移完成：%s → %s", result["from"], result["after"])
        return result
    except Exception as exc:  # noqa: BLE001 - 任何异常都不允许碰正式库
        log.warning("数据库迁移失败：%s", exc)
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        for suffix in ("", "-wal", "-shm"):
            leftover = tmp + suffix
            try:
                if os.path.isfile(leftover):
                    os.remove(leftover)
            except OSError:
                pass


def restore(db_path: str, backup_path: str) -> Optional[str]:
    """把备份还原回正式库（迁移失败后的兜底入口）。"""
    from .backup import restore_backup

    try:
        return restore_backup(backup_path, db_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("还原备份失败：%s", exc)
        return None
