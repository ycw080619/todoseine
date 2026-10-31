"""隐私脱敏（前置正则打码，零第三方依赖）。

文档定位：「归档时默认使用脱敏后文本，命中打码位置自动标注已脱敏」。
这里的边界要分清，否则很容易把脱敏用错地方：

- SQLite 存**原文**，它是待办事实源，本地不外发；
- Markdown 归档、看板展示、日志、云端 AI 请求走**脱敏文本**；
- 白名单群只影响「归档展示」，AI 请求前一律强制脱敏，不认白名单。

对外只暴露三个函数，避免调用方各自拼正则：
    redact(text)                  → (脱敏文本, 命中列表)
    redact_for_ai(text)           → 强制脱敏（跑两遍，防单遍漏网）
    redact_for_display(text, cfg) → 看板展示用，读白名单与「显示原文」开关
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Pattern, Tuple

# (名称, 正则, 保留前几位, 保留后几位)
# 顺序即优先级：身份证必须排在银行卡前面，否则 18 位身份证会被当成银行卡打码。
_BUILTIN: Tuple[Tuple[str, str, int, int], ...] = (
    ("身份证", r"(?<!\d)\d{17}[\dXx](?!\d)", 6, 4),
    ("银行卡", r"(?<!\d)\d{16,19}(?!\d)", 0, 4),
    ("手机号", r"(?<!\d)1[3-9]\d{9}(?!\d)", 3, 4),
    ("邮箱", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", 1, 0),
)

# 脱敏标记：文档要求命中位置标「已脱敏」，展示层靠这个后缀提示用户
MASK_HINT = "（已脱敏）"


@dataclass(frozen=True)
class Hit:
    """一次命中。start/end 是原文下标，便于界面高亮或做审计。"""

    kind: str
    start: int
    end: int
    original: str


def _mask_token(token: str, front: int, back: int) -> str:
    """保留首尾若干位，中间用 * 顶掉。"""
    if front + back >= len(token):
        return "*" * len(token)
    # back=0 时不能写 token[-0:]，那是整串，会变成「打码 + 原文」
    suffix = token[-back:] if back else ""
    return token[:front] + "*" * (len(token) - front - back) + suffix


def _mask_email(token: str) -> str:
    local, at, domain = token.partition("@")
    if not at:
        return _mask_token(token, 1, 0)
    head = local[0] if local else "*"
    return f"{head}***@{domain}"


def _compile_custom(rules: Optional[Iterable[str]]) -> List[Tuple[str, Pattern[str]]]:
    """自定义规则写成 `名称::正则`，坏正则直接跳过，不拖垮整条管线。"""
    out: List[Tuple[str, Pattern[str]]] = []
    for raw in rules or []:
        text = str(raw or "").strip()
        if "::" not in text:
            continue
        name, _, pattern = text.partition("::")
        try:
            out.append((name.strip() or "自定义", re.compile(pattern.strip())))
        except re.error:
            continue
    return out


def redact(text: str, custom_rules: Optional[Iterable[str]] = None) -> Tuple[str, List[Hit]]:
    """按内置规则 + 自定义规则打码，返回脱敏文本与命中列表。

    命中区间按「规则优先级 + 位置」贪心取不重叠的集合：
    身份证先于银行卡，所以 18 位身份证不会被当成银行卡整段打掉；
    自定义规则排在内置规则之后，同区间不会覆盖内置命中。
    """
    if not text:
        return text, []

    spans: List[Tuple[int, int, int, str, str, str]] = []  # start,end,prio,kind,token,masked

    for prio, (kind, pattern, front, back) in enumerate(_BUILTIN):
        for m in re.finditer(pattern, text):
            token = m.group(0)
            masked = _mask_email(token) if kind == "邮箱" else _mask_token(token, front, back)
            spans.append((m.start(), m.end(), prio, kind, token, masked))

    for idx, (name, pattern) in enumerate(_compile_custom(custom_rules)):
        for m in pattern.finditer(text):
            token = m.group(0)
            if not token:
                continue
            spans.append((m.start(), m.end(), len(_BUILTIN) + idx, name, token,
                          _mask_token(token, 0, 0)))

    spans.sort(key=lambda s: (s[0], s[2]))

    parts: List[str] = []
    hits: List[Hit] = []
    cursor = 0
    for start, end, _prio, kind, token, masked in spans:
        if start < cursor:          # 与已接受区间重叠 → 优先级低的让位
            continue
        parts.append(text[cursor:start])
        parts.append(masked)
        hits.append(Hit(kind, start, end, token))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts), hits


def redact_force(text: str, custom_rules: Optional[Iterable[str]] = None) -> str:
    """强制脱敏：跑两遍，确保没有漏网（AI 请求前的二次校验就是它）。"""
    once, _ = redact(text, custom_rules)
    twice, _ = redact(once, custom_rules)
    return twice


def group_whitelisted(session: str, cfg: Dict[str, Any]) -> bool:
    """白名单群：只影响归档/展示，不影响 AI 请求。"""
    privacy = (cfg or {}).get("privacy") or {}
    if not privacy.get("enabled", True):
        return True                      # 总开关关掉 = 全都不打码
    names = [str(x).strip() for x in (privacy.get("whitelist_groups") or [])]
    return bool(session) and session.strip() in names


def redact_for_archive(text: str, session: str, cfg: Dict[str, Any]) -> Tuple[str, List[Hit]]:
    """归档（Markdown）用：白名单群跳过，其余打码。"""
    if group_whitelisted(session, cfg):
        return text, []
    return redact(text, ((cfg or {}).get("privacy") or {}).get("custom_rules"))


def ai_raw_allowed(session: str, cfg: Optional[Dict[str, Any]] = None) -> bool:
    """是否允许把**原文**发往云端 AI。

    文档要求：默认一律脱敏，用户确实想让某些群发明文时，必须单独开启
    开关（界面做二次确认）。这里再加一层群号收窄：

    - allow_raw_ai 关闭（默认）→ 永不放行；
    - allow_raw_ai 开启且 raw_ai_groups 为空 → 全部放行；
    - raw_ai_groups 非空 → 只有列出的群放行，其余仍强制脱敏。
    """
    privacy = (cfg or {}).get("privacy") or {}
    if not privacy.get("allow_raw_ai"):
        return False
    if privacy.get("enabled", True) is False:
        return True                      # 脱敏总开关关闭 = 本来就不打码
    groups = [str(x).strip() for x in (privacy.get("raw_ai_groups") or [])]
    if not groups:
        return True
    return bool(session) and session.strip() in groups


def redact_for_ai(text: str, cfg: Optional[Dict[str, Any]] = None,
                  session: str = "") -> str:
    """云端 AI 用：默认强制脱敏，白名单群不豁免。

    唯一例外是用户在设置里显式打开的「AI 明文」开关（可再按群号收窄），
    该开关需要用户在界面上二次确认后才生效。
    """
    privacy = (cfg or {}).get("privacy") or {}
    if privacy.get("enabled", True) is False:
        return text
    if ai_raw_allowed(session, cfg):
        return text
    return redact_force(text, privacy.get("custom_rules"))


def redact_for_display(text: str, cfg: Dict[str, Any], session: str = "") -> str:
    """看板展示用：默认脱敏并标注；用户显式打开「显示原文」才给原文。"""
    privacy = (cfg or {}).get("privacy") or {}
    if not privacy.get("enabled", True):
        return text
    if privacy.get("board_show_original"):
        return text
    if group_whitelisted(session, cfg):
        return text
    masked, hits = redact(text, privacy.get("custom_rules"))
    return f"{masked} {MASK_HINT}" if hits else masked


class RedactingFilter(logging.Filter):
    """日志脱敏：文档要求「日志始终自动脱敏，白名单群也不例外」。"""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        super().__init__()
        self._cfg = cfg or {}

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - 标准接口名
        privacy = self._cfg.get("privacy") or {}
        if privacy.get("enabled", True) is False:
            return True
        if privacy.get("mask_logs", True) is False:
            return True
        try:
            rules = privacy.get("custom_rules")
            if isinstance(record.msg, str):
                record.msg = redact_force(record.msg, rules)
            if record.args:
                record.args = tuple(
                    redact_force(a, rules) if isinstance(a, str) else a
                    for a in record.args)
        except Exception:  # noqa: BLE001 - 脱敏失败绝不能挡住日志
            pass
        return True


def install_log_filter(cfg: Optional[Dict[str, Any]] = None) -> None:
    """挂到根 logger 及其 handler 上，一次即可；重复调用会先摘掉旧的。

    注意：挂在 logger 上的 filter 只作用于「直接写在该 logger 上的记录」，
    子 logger 通过 propagate 上来的记录不会经过父 logger 的 filter。
    所以这里同时挂到 handler 上，才能覆盖全部日志。
    """
    root = logging.getLogger()
    targets: List[logging.Logger | logging.Handler] = [root, *root.handlers]
    for logging_obj in logging.getLogger(__name__).manager.loggerDict.values():
        if isinstance(logging_obj, logging.Logger):
            targets.extend([logging_obj, *logging_obj.handlers])
    for target in targets:
        for existing in list(target.filters):
            if isinstance(existing, RedactingFilter):
                target.removeFilter(existing)
        target.addFilter(RedactingFilter(cfg))
