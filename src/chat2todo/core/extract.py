"""待办抽取：可解释的规则引擎。

每条待办都会带上 reason 与 confidence，便于人工复核，也便于日后替换成 LLM。
"""

from __future__ import annotations

from typing import Iterable, List

from ..models import Message, MsgType, TodoItem
from .classify import classify

ACTION_WORDS = (
    "记得", "别忘", "不要忘", "安排", "确认", "提交", "报名", "准备",
    "发我", "给我", "处理", "跟进", "回复", "检查", "核对", "务必",
    "需要", "整理", "补一下", "填一下",
)
TIME_WORDS = (
    "今天", "明天", "后天", "本周", "下周", "周一", "周二", "周三", "周四",
    "周五", "周六", "周日", "上午", "下午", "晚上", "点前", "之前", "截止",
    "月底", "尽快", "今天内",
)
SELF_HINTS = ("@我", "麻烦你", "帮我", "你来", "你负责")
QUESTION_HINTS = ("?", "？", "能不能", "可以吗", "行不行", "是否")

TODO_THRESHOLD = 4      # 达到即判为待办
WEAK_THRESHOLD = 3      # 达到则标记为待确认


def _hits(text: str, words: Iterable[str]) -> List[str]:
    return [w for w in words if w in text]


def score_message(msg: Message) -> tuple:
    """返回 (分数, 命中的理由列表)。"""
    text = (msg.content or "").strip()
    if not text or msg.type not in (MsgType.TEXT, MsgType.REPLY):
        return 0, []

    reasons: List[str] = []
    score = 0

    acted = _hits(text, ACTION_WORDS)
    if acted:
        score += 3
        reasons.append("行动词:" + "/".join(acted[:2]))

    timed = _hits(text, TIME_WORDS)
    if timed:
        score += 3
        reasons.append("时间词:" + "/".join(timed[:2]))

    if _hits(text, SELF_HINTS):
        score += 2
        reasons.append("指向我")

    if _hits(text, QUESTION_HINTS):
        score += 1
        reasons.append("疑问")

    if msg.type == MsgType.REPLY or "转账" in text or "红包" in text:
        score += 1
        reasons.append("交易/引用")

    return score, reasons


def extract_todos(messages: Iterable[Message], session: str = "") -> List[TodoItem]:
    """从消息流里抽取出待办，返回按时间排序的结果。"""
    todos: List[TodoItem] = []
    for msg in messages:
        if classify(msg) == "risk":
            continue
        score, reasons = score_message(msg)
        if score < WEAK_THRESHOLD:
            continue
        todos.append(
            TodoItem(
                text=(msg.content or "").strip()[:200],
                session=session,
                sender=msg.sender_name,
                message_ts=msg.timestamp,
                reason="; ".join(reasons),
                confidence=round(min(score / 7.0, 1.0), 2),
                status="pending" if score >= TODO_THRESHOLD else "unconfirmed",
            )
        )
    todos.sort(key=lambda t: t.message_ts)
    return todos
