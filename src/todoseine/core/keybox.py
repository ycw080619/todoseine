"""数据库口令的本机安全存取（DPAPI，零第三方依赖）。

Windows 上用 ctypes 直接调 CryptProtectData / CryptUnprotectData，
密文只有「本机 + 当前用户」能解开，比明文写配置文件安全得多。

存储位置：HKCU\Software\TodoSeine\dbkey（REG_BINARY，DPAPI 密文）。
选注册表而非文件，是因为 PyInstaller 打包的 GUI exe 首次运行时，
Defender 实时防护会拦截对用户目录的文件写入，注册表不受此限制。

兼容旧格式：
  - ~/todoseine/dbkey.dat（新文件路径，DPAPI 密文）
  - ~/Chat2Todo/dbkey.dat（旧文件路径，DPAPI 密文 或 PowerShell SecureString 十六进制）
读取时自动识别并解码，老用户无感迁移到注册表。

非 Windows 平台没有 DPAPI：保存会报错并提示改用环境变量 C2T_DB_KEY。
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ..errors import C2TError

# 注册表路径
_REG_KEY_PATH = r"Software\TodoSeine"
_REG_VALUE_NAME = "dbkey"

# 文件路径（仅用于读取旧密钥，新密钥一律写注册表）
DEFAULT_KEY_FILE = Path.home() / "todoseine" / "dbkey.dat"
_LEGACY_KEY_FILE = Path.home() / "Chat2Todo" / "dbkey.dat"

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
# 注册表读写
# --------------------------------------------------------------------------
def _reg_save(blob: bytes) -> None:
    """把 DPAPI 密文写入 HKCU\\Software\\TodoSeine\\dbkey（REG_BINARY）。"""
    import winreg
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _REG_KEY_PATH, 0, winreg.KEY_WRITE)
    try:
        winreg.SetValueEx(key, _REG_VALUE_NAME, 0, winreg.REG_BINARY, blob)
    finally:
        winreg.CloseKey(key)


def _reg_load() -> Optional[bytes]:
    """从注册表读 DPAPI 密文；不存在返回 None。"""
    import winreg
    try:
        key = winreg.OpenKeyEx(winreg.HKEY_CURRENT_USER, _REG_KEY_PATH, 0, winreg.KEY_READ)
    except FileNotFoundError:
        return None
    try:
        value, _ = winreg.QueryValueEx(key, _REG_VALUE_NAME)
        return value if isinstance(value, bytes) else None
    except FileNotFoundError:
        return None
    finally:
        winreg.CloseKey(key)


def _reg_delete() -> bool:
    """从注册表删除密钥；返回是否删除成功。"""
    import winreg
    try:
        key = winreg.OpenKeyEx(winreg.HKEY_CURRENT_USER, _REG_KEY_PATH, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, _REG_VALUE_NAME)
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------
def key_file_path(path: Optional[str] = None) -> Path:
    """返回口令文件路径（仅用于兼容旧调用方；新密钥存在注册表里）。"""
    if path:
        return Path(path)
    if not DEFAULT_KEY_FILE.exists() and _LEGACY_KEY_FILE.exists():
        return _LEGACY_KEY_FILE
    return DEFAULT_KEY_FILE


def key_location() -> str:
    """返回当前密钥存储位置的可读描述（注册表或文件路径）。"""
    if os.environ.get("C2T_DB_KEY"):
        return "环境变量 C2T_DB_KEY"
    if sys.platform == "win32" and _reg_load() is not None:
        return f"HKCU\\{_REG_KEY_PATH}\\{_REG_VALUE_NAME}"
    for p in (DEFAULT_KEY_FILE, _LEGACY_KEY_FILE):
        if p.exists():
            return str(p)
    return f"HKCU\\{_REG_KEY_PATH}\\{_REG_VALUE_NAME}"


def _load_legacy_file(target: Path) -> str:
    """从旧文件读密钥，兼容 DPAPI 密文和 PowerShell SecureString 两种格式。"""
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


def save_key(key: str, path: Optional[str] = None) -> str:
    """DPAPI 加密后存入注册表；非 Windows 报错并给替代方案。

    返回存储位置描述（注册表路径或文件路径）。
    """
    if sys.platform != "win32":
        raise C2TError("E4002", "本系统没有 DPAPI：请用环境变量 C2T_DB_KEY 或 [db].key_file")
    blob = protect(key)
    _reg_save(blob)
    return f"HKCU\\{_REG_KEY_PATH}\\{_REG_VALUE_NAME}"


def load_key(path: Optional[str] = None) -> str:
    """读出口令。

    优先级：环境变量 C2T_DB_KEY > 注册表 > 旧文件路径。
    文件不存在或解不开时返回空串（调用方负责降级提示）。
    """
    env_key = os.environ.get("C2T_DB_KEY")
    if env_key:
        return env_key
    if sys.platform == "win32":
        blob = _reg_load()
        if blob:
            try:
                return unprotect(blob).strip()
            except Exception:  # noqa: BLE001
                pass
    # 回退到文件（兼容旧数据）
    if path:
        targets = [Path(path)]
    else:
        targets = [DEFAULT_KEY_FILE, _LEGACY_KEY_FILE]
    for target in targets:
        if target.exists():
            try:
                return _load_legacy_file(target)
            except Exception:  # noqa: BLE001
                continue
    return ""


def delete_key(path: Optional[str] = None) -> bool:
    """删除密钥（注册表 + 旧文件）。返回是否删除了任何东西。"""
    deleted = False
    if sys.platform == "win32":
        if _reg_delete():
            deleted = True
    targets = [key_file_path(path)] if path else [DEFAULT_KEY_FILE, _LEGACY_KEY_FILE]
    for target in targets:
        if target.exists():
            try:
                target.unlink()
                deleted = True
            except OSError:
                pass
    return deleted


def has_key(path: Optional[str] = None) -> bool:
    return bool(os.environ.get("C2T_DB_KEY")) or bool(load_key(path))
