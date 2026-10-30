"""截图归档（可选功能，默认关闭）。

文档口径：截图归档默认关闭，开启后单独目录存储，保留期可配置。

与「OCR 临时截图不落盘」并不冲突——OCR 用的图始终只在内存里处理；
只有用户显式打开 ``archive.screenshots`` 后，手动「截图建待办」的那张图
才会写进独立的 ``screenshots/`` 目录，并按 ``archive.screenshot_days`` 定期清理。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict

log = logging.getLogger("chat2todo.screenshot")

SHOT_SUBDIR = "screenshots"


def _archive_base(cfg: Dict[str, Any], db_path: str) -> str:
    from .archive import default_archive_dir

    archive = dict(cfg.get("archive") or {})
    return str(archive.get("dir") or "") or default_archive_dir(
        db_path or str(cfg.get("db_path") or ""))


def screenshot_dir(cfg: Dict[str, Any], db_path: str = "") -> str:
    """截图归档目录；开关关闭或没配库路径时返回空串。"""
    archive = dict(cfg.get("archive") or {})
    if archive.get("screenshots", False) is not True:
        return ""
    base = _archive_base(cfg, db_path)
    return os.path.join(base, SHOT_SUBDIR) if base else ""


def save(png_bytes: bytes, cfg: Dict[str, Any], db_path: str = "",
         ts: int | None = None) -> str:
    """把一张截图写进归档目录，返回落盘路径；开关关闭时返回空串。

    写失败只记日志，不影响截图建待办主流程。
    """
    if not png_bytes:
        return ""
    folder = screenshot_dir(cfg, db_path)
    if not folder:
        return ""
    stamp = int(ts or time.time())
    name = time.strftime("%Y%m%d-%H%M%S", time.localtime(stamp)) + f"-{stamp % 1000:03d}.png"
    path = os.path.join(folder, name)
    try:
        os.makedirs(folder, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(png_bytes)
        os.replace(tmp, path)           # 原子替换，避免半截文件
    except OSError as exc:
        log.warning("截图归档失败 %s：%s", path, exc)
        return ""
    return path


def _ts_from_name(name: str) -> int:
    """从 ``20261030-082009-123.png`` 里取时间戳；解析不了返回 0。"""
    import re

    m = re.match(r"(\d{8})-(\d{6})-\d+\.png$", name, re.IGNORECASE)
    if not m:
        return 0
    try:
        return int(time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")))
    except (ValueError, OverflowError):
        return 0


def purge(cfg: Dict[str, Any], db_path: str = "", now: int | None = None) -> int:
    """删除超过保留期的截图，返回删除数量。开关关闭时不动任何文件。

    判定用文件名里的时间戳而不是文件 mtime——复制/同步会改写 mtime，
    但文件名是写入那一刻定死的，留档含义更准。
    """
    folder = screenshot_dir(cfg, db_path)
    if not folder or not os.path.isdir(folder):
        return 0
    days = int(dict(cfg.get("archive") or {}).get("screenshot_days") or 7)
    cutoff = int(now or time.time()) - max(1, days) * 86400
    dropped = 0
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if not os.path.isfile(path) or not name.lower().endswith(".png"):
            continue
        stamp = _ts_from_name(name) or int(os.path.getmtime(path))
        try:
            if stamp < cutoff:
                os.remove(path)
                dropped += 1
        except OSError as exc:
            log.warning("清理旧截图失败 %s：%s", path, exc)
    if dropped:
        log.info("截图归档清理 %d 张（超过 %d 天）", dropped, days)
    return dropped
