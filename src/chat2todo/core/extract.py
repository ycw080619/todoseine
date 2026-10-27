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


NOTICE_WORDS = (
    "@全体成员", "全体成员", "通知", "收取", "提交", "报到", "务必", "必须",
)
TECH_HINTS = ("喵", "哈哈", "笑死", "草", "选课", "驱动", "跑分", "开播", "崩巴", "呜呜", "好耶",
              "666", "233", "哎", "emmm", "xd", "awsl", "吃瓜", "摸鱼")


def _latin_ratio(text: str) -> float:
    if not text:
        return 0.0
    return sum(1 for ch in text if ord(ch) < 128) / len(text)


def _notice_bonus(text: str) -> tuple:
    """@全体成员 / 收取 / 提交 这类「明确要求一群人」的消息，应当加权而不是被淹没。"""
    score = 0
    reasons = []
    if "@全体成员" in text or "全体成员" in text:
        score += 4
        reasons.append("通知全体")
    elif [w for w in NOTICE_WORDS if w in text]:
        score += 2
        reasons.append("通知/要求")
    return score, reasons


def _has_repeat(text: str) -> bool:
    """同一字符连续出现 3 次以上，多半是「哈哈哈哈」「崩巴崩巴」这类玩梗。"""
    prev, run = "", 0
    for ch in text:
        if ch == prev:
            run += 1
            if run >= 2:      # 含首次共 3 次
                return True
        else:
            prev, run = ch, 0
    return False


def _noise_penalty(text: str) -> tuple:
    """技术闲聊、代码、表情语气——不该进待办。"""
    score = 0
    reasons = []
    if _latin_ratio(text) > 0.5:
        score -= 3
        reasons.append("疑似代码/技术闲聊")
    if [w for w in TECH_HINTS if w in text]:
        score -= 1
        reasons.append("语气词")
    if _has_repeat(text) and "@全体成员" not in text:
        score -= 1
        reasons.append("重复字/玩梗")
    # 太短的多半是「晚上好」「我记得是这样」这类寒暄或附和，不该算待办
    if len(text) < 8 and "@全体成员" not in text:
        score -= 2
        reasons.append("过短")
    return score, reasons

TODO_THRESHOLD = 4      # 达到即判为待办
WEAK_THRESHOLD = 3      # 达到则标记为待确认


def _hits(text: str, words: Iterable[str]) -> List[str]:
    return [w for w in words if w in text]


def score_message(msg: Message, weights_map: dict = None) -> tuple:
    """返回 (分数, 命中的理由列表)。

    weights_map 可选：{(chat_id, sender_name): {reason_tag: adjustment}}
    对每个命中的 reason 标签额外叠加用户校准权重 [-0.5, 0.5]。
    """
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

    bonus, bonus_reasons = _notice_bonus(text)
    score += bonus
    reasons.extend(bonus_reasons)

    penalty, penalty_reasons = _noise_penalty(text)
    score += penalty
    reasons.extend(penalty_reasons)

    # 用户校准权重叠加：同群同 sender 上被 reject 过的 reason_tag 降权，被 confirm 过的升权
    if weights_map:
        ck = (msg.chat_id or "", msg.sender_name or "")
        local_w = weights_map.get(ck, {})
        if local_w:
            for tag in reasons:
                for key, adj in local_w.items():
                    if key and key in tag:
                        score = round(score + adj * 7.0, 2)   # 权重域 [-3.5, 3.5]
                        break

    return score, reasons


def extract_todos(messages: Iterable[Message], session: str = "",
                  weights_map: dict = None) -> List[TodoItem]:
    """从消息流里抽取出待办，返回按时间排序的结果。"""
    todos: List[TodoItem] = []
    for msg in messages:
        if classify(msg) == "risk":
            continue
        score, reasons = score_message(msg, weights_map)
        if score < WEAK_THRESHOLD:
            continue
        todos.append(
            TodoItem(
                text=(msg.content or "").strip()[:200],
                session=session,
                sender=msg.sender_name,
                message_ts=msg.timestamp,
                reason=("★重点 · " if getattr(msg, "star", False) else "") + "; ".join(reasons),
                confidence=round(min(max(score, 0) / 7.0, 1.0), 2),
                status="pending" if score >= TODO_THRESHOLD else "unconfirmed",
            )
        )
    todos.sort(key=lambda t: t.message_ts)
    return todos
