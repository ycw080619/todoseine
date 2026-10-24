"""配置加载（零第三方依赖）。

查找顺序：
1. 命令行 --config 指定的文件
2. 环境变量 C2T_CONFIG
3. ./c2t.toml、./chat2todo.toml
4. ~/.config/chat2todo/config.toml

Python 3.11+ 用标准库 tomllib；更低版本退回一个只认 `key = value` 的极简解析器。
无论如何，缺省值永远可用——没有配置文件也能跑。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

DEFAULTS: Dict[str, Any] = {
    "db_path": "c2t.db",
    "export_dir": "out",
    "todo_threshold": 4,
    "pending_threshold": 3,
    "strict_dedupe": False,
    "obsidian_vault": "",
    "language": "zh",
    "log_level": "info",
}

DB_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "path": "",
    "module": "",
    "args": "",
    "key": "",
    "key_file": "",
    "page_size": 4096,
}

AI_DEFAULTS: Dict[str, Any] = {    "enabled": False,
    "review": False,
    "merge": False,
    "intent": False,
    "rewrite": False,
    "base_url": "",
    "api_key": "",
    "model": "gpt-4o-mini",
    "timeout": 15,
}

_HOME_CONFIG = os.path.join(
    os.path.expanduser("~"), ".config", "chat2todo", "config.toml"
)
_CANDIDATES = ("c2t.toml", "chat2todo.toml", _HOME_CONFIG)


class ConfigError(Exception):
    """配置文件无法解析。"""


def _mini_parse(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as fh:
        return _mini_parse_text(fh.read())


def _mini_parse_text(text: str) -> Dict[str, Any]:
    """极简 TOML 子集：支持 [section] 与 key = value，忽略注释。"""
    data: Dict[str, Any] = {}
    section = ""
    for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            data[f"{section}.{key}" if section else key] = _coerce(value)
    return _nest(data)


def _coerce(value: str) -> Any:
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


def _nest(flat: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in flat.items():
        if "." in key:
            head, rest = key.split(".", 1)
            out.setdefault(head, {})
            if isinstance(out[head], dict):
                out[head][rest] = value
        else:
            out[key] = value
    return out


def _read(path: str) -> Dict[str, Any]:
    # 显式按 utf-8-sig 解码：Windows 记事本 / PowerShell 写出的 TOML 常带 BOM，
    # 而 tomllib 不接受 BOM（会报 Invalid statement at line 1 column 1）。
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover - 低版本回退
        return _mini_parse_text(text)
    try:
        return tomllib.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"{path}: {exc}") from exc


def find_file(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit if os.path.exists(explicit) else None
    env = os.environ.get("C2T_CONFIG")
    if env and os.path.exists(env):
        return env
    for candidate in _CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def load(path: Optional[str] = None) -> Dict[str, Any]:
    """返回合并后的配置；找不到文件时返回纯默认值。"""
    cfg: Dict[str, Any] = dict(DEFAULTS)
    cfg["ai"] = dict(AI_DEFAULTS)
    cfg["db"] = dict(DB_DEFAULTS)

    real = find_file(path)
    if real:
        raw = _read(real)
        cfg["config_file"] = real
        for key, value in raw.items():
            if key in ("ai", "db") and isinstance(value, dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    else:
        cfg["config_file"] = ""

    if os.environ.get("C2T_DB"):
        cfg["db_path"] = os.environ["C2T_DB"]
    if os.environ.get("C2T_DB_MODULE"):
        cfg["db"]["module"] = os.environ["C2T_DB_MODULE"]
    if os.environ.get("C2T_DB_ARGS"):
        cfg["db"]["args"] = os.environ["C2T_DB_ARGS"]
    if os.environ.get("C2T_DB_KEY"):
        cfg["db"]["key"] = os.environ["C2T_DB_KEY"]
    return cfg


def sample_toml() -> str:
    """`c2t doctor --write-sample` 用：一份可直接编辑的注释版配置。"""
    return """# Chat2Todo 配置（放在当前目录或 ~/.config/chat2todo/config.toml）
db_path          = "c2t.db"   # 数据库位置
export_dir       = "out"      # 导出目录
todo_threshold   = 4          # 判为待办的最低分
pending_threshold= 3          # 判为待确认的最低分
strict_dedupe    = false      # true 则不合并“同秒同文”的消息
obsidian_vault   = ""         # 填了就在导出时用 [[双链]] 格式
language         = "zh"
log_level        = "info"

[ai]
enabled = false               # 总开关；关闭时下面四项一律不生效
review  = false               # 边界复核
merge   = false               # 同事实归并
intent  = false               # 说话人意图
rewrite = false               # 待办改写
base_url = ""                 # 例：https://api.openai.com/v1
api_key  = ""                 # 或用环境变量 C2T_AI_KEY
model    = "gpt-4o-mini"
timeout  = 15

[db]
enabled  = false              # 数据库只读通道总开关
path     = ""                 # nt_msg.db 路径；留空则自动探测
module   = ""                 # 社区解密工具路径（推荐）
args     = ""                 # 传给社区模块的额外参数
key      = ""                 # 内置读取器用（或用环境变量 C2T_DB_KEY）
key_file = ""                 # 从文件读 key
page_size= 4096
"""
