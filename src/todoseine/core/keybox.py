"""数据库口令的本机安全存取（DPAPI，零第三方依赖）。

Windows 上用 ctypes 直接调 CryptProtectData / CryptUnprotectData，
密文只有「本机 + 当前用户」能解开，比明文写配置文件安全得多。

兼容旧格式：tools/qq_db_export/sync.py 用 PowerShell SecureString 存的
dbkey.dat（一长串十六进制文本），这里读取时自动识别并解码，老用户无感迁移。

非 Windows 平台没有 DPAPI：保存会报错并提示改用环境变量 C2T_DB_KEY。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ..errors import C2TError

DEFAULT_KEY_FILE = Path.home() / "Chat2Todo" / "dbkey.dat"

# PowerShell SecureString 导出形态：一长串十六进制
_LEGACY_HEX = re.compile(r"^[0-9a-fA-F]{100,}$")


# --------------------------------------------------------------------------
# DPAPI 封装
# --------------------------------------------------------------------------
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not func(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise C2TError("E4003", "DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect(text: str) -> bytes:
    return _dpapi(text.encode("utf-8"), protect=True)


def unprotect(data: bytes) -> str:
    return _dpapi(data, protect=False).decode("utf-8")


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------
def key_file_path(path: Optional[str] = None) -> Path:
    return Path(path) if path else DEFAULT_KEY_FILE


def save_key(key: str, path: Optional[str] = None) -> Path:
    """DPAPI 加密后落盘；非 Windows 报错并给替代方案。"""
    if sys.platform != "win32":
        raise C2TError("E4002", "本系统没有 DPAPI：请用环境变量 C2T_DB_KEY 或 [db].key_file")
    target = key_file_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(protect(key))
    return target


def _load_legacy_ps(blob: bytes) -> str:
    """旧格式：PowerShell SecureString 导出的十六进制串，仍交给 PowerShell 解。"""
    hex_text = blob.decode("ascii").strip()
    cmd = (
        f"$s = '{hex_text}' | ConvertTo-SecureString;"
        "[Runtime.InteropServices.Marshal]::PtrToStringAuto("
        "[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True, text=True, timeout=30,
    )
    return out.stdout.strip()


def load_key(path: Optional[str] = None) -> str:
    """读出口令；文件不存在或解不开时返回空串（调用方负责降级提示）。"""
    target = key_file_path(path)
    if not target.exists() or sys.platform != "win32":
        return ""
    blob = target.read_bytes()
    if not blob:
        return ""
    try:
        return unprotect(blob).strip()
    except Exception:  # noqa: BLE001 - 可能是旧格式，走 PowerShell 兜底
        pass
    if _LEGACY_HEX.match(blob.decode("ascii", "ignore").strip()):
        try:
            return _load_legacy_ps(blob)
        except Exception:  # noqa: BLE001
            return ""
    return ""


def delete_key(path: Optional[str] = None) -> bool:
    target = key_file_path(path)
    if target.exists():
        target.unlink()
        return True
    return False


def has_key(path: Optional[str] = None) -> bool:
    return bool(os.environ.get("C2T_DB_KEY")) or bool(load_key(path))
