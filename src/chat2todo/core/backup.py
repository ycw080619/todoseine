"""每日自动备份 + Windows DPAPI 加密（零第三方依赖）。

文档要求：每日自动备份全量数据，备份文件加密存储。
这里的边界：
  * 只备份 SQLite 库文件本身（待办 / 消息的唯一结构化事实源）；
  * 备份前先做 WAL checkpoint，避免拿到半截日志；
  * 用 Windows DPAPI 按当前用户加密，换机器 / 换用户都解不开；
  * 非 Windows 或 DPAPI 不可用时退化为明文 .db 备份，并在日志里说明。
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import sqlite3
import time
from typing import Any, Dict, Optional

log = logging.getLogger("chat2todo.backup")

_MAGIC = b"C2TDPAPI1\n"          # 加密备份文件头，还原时靠它判断是否需要解密
_STATE = ".last_backup"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _crypt32():
    if os.name != "nt":
        return None
    try:
        return ctypes.windll.crypt32
    except OSError:
        return None


def dpapi_protect(data: bytes) -> Optional[bytes]:
    """用当前 Windows 用户的 DPAPI 密钥加密；失败返回 None。"""
    crypt32 = _crypt32()
    if crypt32 is None:
        return None
    try:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DataBlob()
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:  # noqa: BLE001 - DPAPI 不可用就退化
        return None


def dpapi_unprotect(data: bytes) -> Optional[bytes]:
    """解密 dpapi_protect 的输出（备份还原脚本用）。"""
    crypt32 = _crypt32()
    if crypt32 is None:
        return None
    try:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DataBlob()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:  # noqa: BLE001
        return None


def default_backup_dir(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")


def _checkpoint(db_path: str) -> None:
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def _prune(backup_dir: str, keep: int) -> None:
    """只保留最近 keep 份；keep<=0 表示不清理。"""
    if keep <= 0:
        return
    try:
        names = [n for n in os.listdir(backup_dir)
                 if n.startswith("qq-") and (n.endswith(".db") or n.endswith(".db.enc"))]
    except OSError:
        return
    names.sort()
    for name in names[:-keep] if len(names) > keep else []:
        try:
            os.remove(os.path.join(backup_dir, name))
        except OSError:
            pass


def backup_database(db_path: str, backup_dir: str = "", keep: int = 7,
                    encrypt: bool = True) -> Optional[str]:
    """备份一次库文件，返回备份路径；库不存在或失败返回 None。"""
    if not db_path or not os.path.isfile(db_path):
        return None
    target_dir = backup_dir or default_backup_dir(db_path)
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as exc:
        log.warning("创建备份目录失败：%s", exc)
        return None

    _checkpoint(db_path)
    try:
        with open(db_path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        log.warning("读取库文件失败，跳过备份：%s", exc)
        return None

    payload = dpapi_protect(raw) if encrypt else None
    encrypted = payload is not None
    if not encrypted:
        payload = raw
        if encrypt:
            log.warning("DPAPI 不可用，本次备份以明文保存")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"qq-{stamp}.db" + (".enc" if encrypted else "")
    path = os.path.join(target_dir, name)
    try:
        with open(path, "wb") as fh:
            if encrypted:
                fh.write(_MAGIC)
            fh.write(payload)
    except OSError as exc:
        log.warning("写入备份失败：%s", exc)
        return None
    _prune(target_dir, max(1, int(keep)))
    return path


def _state_path(backup_dir: str) -> str:
    return os.path.join(backup_dir, _STATE)


def daily_backup(cfg: Dict[str, Any]) -> Optional[str]:
    """一天最多备份一次；成功返回备份路径，今天已备份返回 None。"""
    backup = cfg.get("backup") or {}
    if backup.get("enabled", True) is False:
        return None
    db_path = str(cfg.get("db_path") or "")
    if not db_path or not os.path.isfile(db_path):
        return None
    target = str(backup.get("dir") or "") or default_backup_dir(db_path)
    state = _state_path(target)
    today = time.strftime("%Y-%m-%d")
    try:
        with open(state, "r", encoding="utf-8") as fh:
            if fh.read().strip() == today:
                return None
    except OSError:
        pass

    path = backup_database(
        db_path, target,
        keep=int(backup.get("keep") or 7),
        encrypt=bool(backup.get("encrypt", True)),
    )
    if path:
        try:
            os.makedirs(target, exist_ok=True)
            with open(state, "w", encoding="utf-8") as fh:
                fh.write(today)
        except OSError:
            pass
        log.info("每日备份完成：%s", path)
    return path


def restore_backup(backup_path: str, db_path: str) -> bool:
    """从备份还原库文件；加密备份会自动解密。调用方需先关掉所有数据库连接。"""
    try:
        with open(backup_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return False
    if raw.startswith(_MAGIC):
        plain = dpapi_unprotect(raw[len(_MAGIC):])
        if plain is None:
            log.warning("备份解密失败（换过用户或机器？）")
            return False
        raw = plain
    tmp = db_path + ".restore.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(raw)
        shutil.move(tmp, db_path)
    except OSError:
        return False
    return True
