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
from typing import Any, Dict, List, Optional

DEFAULTS: Dict[str, Any] = {
    # 与控制台、看板、文档保持同一个库：%USERPROFILE%\Chat2Todo\qq.db。
    # 之前是相对路径 "c2t.db"，CLI 换个工作目录就会凭空多出一个空库，看着像数据丢了。
    "db_path": os.path.join(os.path.expanduser("~"), "Chat2Todo", "qq.db"),
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
    "days": 0,
}

FILTER_DEFAULTS = {
    "ignore_groups": [],      # 免打扰：这些群号或 QQ 号（如群机器人）的消息完全不统计
    "star_groups": [],        # 特别关心：这些群号或 QQ 号的消息标记为重点
    "star_senders": [],       # 特别关心（旧写法）：这些 QQ 号发的消息标记为重点
}

# 实时监听（qt 看板常驻采集）
UIA_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "interval": 5,            # 两轮抓取之间的间隔（秒）
    "ocr_enabled": True,
    "session": "QQ实时",      # OCR 通道读不出群名，统一用这个来源名
    "hwnd": 0,                # 0 = 自动取面积最大的 QQ 主窗口
}

# 看板外观 / 位置记忆
DASH_DEFAULTS: Dict[str, Any] = {
    "top_n": 5,
    "width": 560,
    "height": 700,
    "x": None,
    "y": None,
    "radius": 14,
    "border": True,
    "clock": True,
    "anim": True,
    "material": True,
    "snap": True,
    "lock": False,
}

# 全局热键
HOTKEY_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "show": "ctrl+alt+d",     # 唤出 / 收起看板
    "snap": "ctrl+shift+s",   # 截图建待办
}

# 通知
NOTIFY_DEFAULTS: Dict[str, Any] = {
    "new_todo": True,
    "due": True,
    "due_within": 3600,       # 提前多少秒提醒到期项
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
# 只认固定的用户目录路径。当前工作目录的 c2t.toml 默认不加载：
# 在别人给的目录/下载目录里跑一次，就可能被投毒的配置改掉 base_url，
# 聊天原文与 API key 会被悄悄发出去。需要时用 C2T_TRUST_CWD=1 显式开启。
_CANDIDATES = (_HOME_CONFIG,)
_CWD_CANDIDATES = ("c2t.toml", "chat2todo.toml")


class ConfigError(Exception):
    """配置文件无法解析。"""


def _mini_parse(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as fh:
        return _mini_parse_text(fh.read())


def _mini_parse_text(text: str) -> Dict[str, Any]:
    """极简 TOML 子集：支持 [section]、key = value、内联数组，忽略注释。"""
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
        return _unescape(value[1:-1])
    if value.startswith("[") and value.endswith("]"):
        # 内联数组：ignore_groups = ["123", "456"] / star_groups = []
        body = value[1:-1].strip()
        return [_coerce(part) for part in _split_list(body)]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)          # 0.92 这类阈值不能留成字符串
    except ValueError:
        return value


def _unescape(text: str) -> str:
    """还原 save() 写出的转义（\\\\ / \\" / \\n 等）。

    Python 3.11+ 走 tomllib 不需要这段；3.9/3.10 走极简解析器时必须还原，
    否则 Windows 路径会被读成双反斜杠、库直接打不开。
    """
    import re

    table = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
             '"': '"', "\\": "\\"}
    return re.sub(r"\\(.)", lambda m: table.get(m.group(1), m.group(1)), text)


def _split_list(body: str) -> List[str]:
    """按逗号切内联数组，但不动引号里的逗号（来源名里可能有）。"""
    parts: List[str] = []
    buf: List[str] = []
    quote = ""
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


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
    if os.environ.get("C2T_TRUST_CWD") == "1":
        for candidate in _CWD_CANDIDATES:
            if os.path.exists(candidate):
                return candidate
    for candidate in _CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def load(path: Optional[str] = None) -> Dict[str, Any]:
    """返回合并后的配置；找不到文件时返回纯默认值。"""
    from .semantic import VECTOR_DEFAULTS

    cfg: Dict[str, Any] = dict(DEFAULTS)
    cfg["ai"] = dict(AI_DEFAULTS)
    cfg["db"] = dict(DB_DEFAULTS)
    cfg["vector"] = dict(VECTOR_DEFAULTS)
    cfg["filter"] = dict(FILTER_DEFAULTS)
    cfg["uia"] = dict(UIA_DEFAULTS)
    cfg["dash"] = dict(DASH_DEFAULTS)
    cfg["hotkey"] = dict(HOTKEY_DEFAULTS)
    cfg["notify"] = dict(NOTIFY_DEFAULTS)

    real = find_file(path)
    if real:
        raw = _read(real)
        cfg["config_file"] = real
        for key, value in raw.items():
            if key in ("ai", "db", "vector", "filter", "uia", "dash",
                       "hotkey", "notify") and isinstance(value, dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    else:
        cfg["config_file"] = ""

    if os.environ.get("C2T_DB"):
        cfg["db_path"] = os.environ["C2T_DB"]
    if not str(cfg.get("db_path") or "").strip():
        # 配置文件里留空（或用户写错成空串）时回落到默认用户目录库
        cfg["db_path"] = DEFAULTS["db_path"]
    if os.environ.get("C2T_DB_MODULE"):
        cfg["db"]["module"] = os.environ["C2T_DB_MODULE"]
    if os.environ.get("C2T_DB_ARGS"):
        cfg["db"]["args"] = os.environ["C2T_DB_ARGS"]
    if os.environ.get("C2T_DB_KEY"):
        cfg["db"]["key"] = os.environ["C2T_DB_KEY"]
    if os.environ.get("C2T_VECTOR_URL"):
        cfg["vector"]["base_url"] = os.environ["C2T_VECTOR_URL"]
    if os.environ.get("C2T_VECTOR_MODEL"):
        cfg["vector"]["model"] = os.environ["C2T_VECTOR_MODEL"]
    if os.environ.get("C2T_VECTOR_OFF"):
        cfg["vector"]["enabled"] = False
    # TOML 里写成 enabled = "false"（多打了引号）时 bool("false") is True，
    # AI 会被意外开启并开始外发聊天原文，这里统一按真值语义纠正一次。
    for section in ("ai", "vector", "db"):
        if section in cfg and "enabled" in cfg[section]:
            cfg[section]["enabled"] = _as_bool(cfg[section]["enabled"], False)
    return cfg


def save(cfg: Dict[str, Any], path: Optional[str] = None) -> str:
    """把整份配置写回 TOML（默认 ~/.config/chat2todo/config.toml）。

    只写已知段落，`config_file` 这类运行期字段不落盘；None 直接跳过，
    读回时自然回落到默认值。返回实际写入的路径。
    """
    target = path or _HOME_CONFIG
    os.makedirs(os.path.dirname(target), exist_ok=True)

    sections = ("ai", "vector", "db", "filter", "uia", "dash", "hotkey", "notify")
    lines = ["# Chat2Todo 配置（由设置面板写入，可手工编辑）"]

    def emit(prefix: str, key: str, value: Any) -> str:
        k = f"{prefix}{key}"
        if isinstance(value, bool):
            return f"{k} = {'true' if value else 'false'}"
        if isinstance(value, (int, float)):
            return f"{k} = {value}"
        if isinstance(value, (list, tuple)):
            inner = ", ".join(_quote(str(v)) for v in value)
            return f"{k} = [{inner}]"
        return f"{k} = {_quote(str(value))}"

    for key, value in cfg.items():
        if key in sections or value is None or key == "config_file":
            continue
        if isinstance(value, dict):
            continue
        lines.append(emit("", key, value))

    for section in sections:
        data = cfg.get(section)
        if not isinstance(data, dict):
            continue
        lines.append("")
        lines.append(f"[{section}]")
        for key, value in data.items():
            if value is None:
                continue
            lines.append(emit("", key, value))

    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return target


def _quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'


_FALSE_WORDS = ("", "0", "false", "no", "off", "none", "null")


def _as_bool(value: Any, default: bool = False) -> bool:
    """把配置里的开关值解析成 bool（字符串按真值语义）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_WORDS
    return default


def sample_toml() -> str:
    """`c2t doctor --write-sample` 用：一份可直接编辑的注释版配置。"""
    return r"""# Chat2Todo 配置（放在当前目录或 ~/.config/chat2todo/config.toml）
db_path          = ""         # 留空 = %USERPROFILE%\Chat2Todo\qq.db（与控制台同一个库）
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

[vector]
enabled       = true                     # 语义去重 + 公告过滤；Ollama 不在时自动退回纯规则
base_url      = "http://127.0.0.1:11434" # 本机 Ollama；也可设环境变量 C2T_VECTOR_URL
model         = "bge-m3"                 # 需先 ollama pull bge-m3
sim_threshold = 0.92                     # 语义去重阈值（越高越保守）
ann_threshold = 0.80                     # 公告过滤阈值

[db]
enabled  = false              # 数据库只读通道总开关
path     = ""                 # nt_msg.db 路径；留空则自动探测
key      = ""                 # 不推荐明文写这里；优先 `c2t db-key --save`（DPAPI 加密）
key_file = ""                 # 或从文件读 key；也可用环境变量 C2T_DB_KEY
days     = 0                  # db-import 默认只取最近 N 天，0 为全部
module   = ""                 # 备用：社区解密工具路径（无 sqlcipher3 时用）
args     = ""                 # 传给社区模块的额外参数
page_size= 4096
"""
