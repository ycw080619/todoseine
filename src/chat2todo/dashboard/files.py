"""看板的文件小工具：时间格式化、从文本识别文件名、在 QQ 下载目录里找文件。"""

from __future__ import annotations

import time
from typing import List

def _fmt_ts(ts) -> str:
    """把时间戳格式化成 10-25 13:40；拿不到就原样返回。"""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return str(ts or "")


# 常见下载文件扩展名：从待办文字 / 原文消息里识别「QQ 下载的文件」
_FILE_EXTS = ("zip|rar|7z|tar|gz|docx?|xlsx?|pptx?|pdf|txt|md|csv|json|"
              "png|jpe?g|gif|webp|mp4|mov|avi|mp3|wav|exe|msi|apk|psd|ai|fig")


def _extract_filenames(texts) -> List[str]:
    """从若干段文本里挑出像文件名的词（保留顺序、去重）。"""
    import re as _re

    out: List[str] = []
    for text in texts:
        for m in _re.finditer(r"[\w（）()\-—]{1,60}\.(?:" + _FILE_EXTS + r")\b",
                              text or "", _re.IGNORECASE):
            name = m.group(0).strip(" .")
            if name and name not in out:
                out.append(name)
    return out


def _documents_dir():
    """真实「文档」目录（用户可能把它重定向出 C 盘，读注册表拿准）。"""
    from pathlib import Path

    try:
        import winreg

        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
        ) as k:
            return Path(winreg.QueryValueEx(k, "Personal")[0])
    except Exception:  # noqa: BLE001
        return Path.home() / "Documents"


def _find_downloaded_file(names) -> str:
    """在 QQ 下载目录里找这些文件名，返回最新一个的路径；找不到返回空串。

    覆盖老 QQ 的 Tencent Files/<qq>/FileRecv、NTQQ 的 nt_data/File 分层目录，
    以及系统「下载」目录；条目数封顶，避免超大目录拖慢。
    """
    from itertools import islice
    from pathlib import Path

    if not names:
        return ""
    lowered = {n.lower() for n in names}
    hits = []
    for root in (_documents_dir() / "Tencent Files", Path.home() / "Downloads"):
        if not root.exists():
            continue
        try:
            for p in islice(root.rglob("*"), 60000):
                if p.is_file() and p.name.lower() in lowered:
                    hits.append(p)
        except Exception:  # noqa: BLE001
            continue
    if not hits:
        return ""
    hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(hits[0])
