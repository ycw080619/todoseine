"""数据维护：SQLite checkpoint / VACUUM / 一键清空（零第三方依赖）。

「一键清空」只删程序自身生成的内容，边界写死在这里：
  删：数据库 + WAL/SHM、Markdown 归档目录、备份目录、日志与缓存、
      插件**配置**与崩溃计数
  不删：插件本体（tools/plugins 等目录）、用户 Obsidian 库里的其他笔记、
        用户自己放进归档目录之外的任何文件

清空前先做 wal_checkpoint(TRUNCATE) 并删除 -wal/-shm，再做 VACUUM，
确保敏感数据不会残留在空闲页里被磁盘取证捞出来。
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from typing import Any, Dict, List

log = logging.getLogger("todoseine.maintenance")


def checkpoint_and_vacuum(db_path: str) -> None:
    """把 WAL 并回主库并重写库文件，擦掉删除后的残留页。"""
    if not db_path or not os.path.isfile(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("checkpoint/VACUUM 失败：%s", exc)


def _home_dir() -> str:
    return os.path.expanduser("~/.chat2todo")


def _archive_dir(cfg: Dict[str, Any], db_path: str) -> str:
    configured = str((cfg.get("archive") or {}).get("dir") or "")
    if configured:
        return configured
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "archive") \
        if db_path else ""


def _backup_dir(cfg: Dict[str, Any], db_path: str) -> str:
    configured = str((cfg.get("backup") or {}).get("dir") or "")
    if configured:
        return configured
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups") \
        if db_path else ""


def wipe_all(cfg: Dict[str, Any], home_dir: str = "") -> List[str]:
    """按文档口径清空程序生成内容，返回实际删掉的路径列表。

    调用前必须先停掉采集 / 写入线程并关闭数据库连接。
    home_dir 仅测试用，正常调用留空即用 ~/.chat2todo。
    """
    db_path = str(cfg.get("db_path") or "")
    removed: List[str] = []

    # 1) 数据库：先 checkpoint + VACUUM，再删库与 WAL/SHM
    checkpoint_and_vacuum(db_path)
    for path in (db_path, db_path + "-wal", db_path + "-shm"):
        if path and os.path.isfile(path):
            try:
                os.remove(path)
                removed.append(path)
            except OSError as exc:
                log.warning("删除数据库文件失败 %s：%s", path, exc)

    # 2) 归档 / 备份目录（整目录都是程序生成的）
    for folder in (_archive_dir(cfg, db_path), _backup_dir(cfg, db_path)):
        if folder and os.path.isdir(folder):
            try:
                shutil.rmtree(folder)
                removed.append(folder)
            except OSError as exc:
                log.warning("删除目录失败 %s：%s", folder, exc)

    # 3) 日志 / 缓存 / 去重状态 / 插件配置；插件本体保留
    home = home_dir or _home_dir()
    if os.path.isdir(home):
        for name in os.listdir(home):
            if name in ("plugins", "uia_selectors"):
                continue            # 插件本体与用户选择器配置保留
            path = os.path.join(home, name)
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                removed.append(path)
            except OSError as exc:
                log.warning("删除失败 %s：%s", path, exc)
    log.info("一键清空完成：共删除 %d 项", len(removed))
    return removed
