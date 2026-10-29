"""待办抽取：可解释的规则引擎。

每条待办都会带上 reason 与 confidence，便于人工复核，也便于日后替换成 LLM。
"""

from __future__ import annotations

import re
from typing import Iterable, List

from ..models import Message, MsgType, TodoItem
from .classify import classify

ACTION_WORDS = (
    "记得", "别忘", "不要忘", "安排", "确认", "提交", "报名", "准备",
    "发我", "给我", "处理", "跟进", "回复", "检查", "核对", "务必",
    "需要", "整理", "补一下", "填一下",
    "交给", "递交", "上交", "交到", "发一下", "传一下",
    "做完", "做完再", "写一下", "写好", "填好", "报一下",
)
TIME_WORDS = (
    "今天", "明天", "后天", "本周", "下周", "周一", "周二", "周三", "周四",
    "周五", "周六", "周日", "上午", "下午", "晚上", "点前", "之前", "截止",
    "月底", "尽快", "今天内",
)
SELF_HINTS = ("@我", "麻烦你", "帮我", "你来", "你负责", "帮忙", "协助")
QUESTION_HINTS = ("?", "？", "能不能", "可以吗", "行不行", "是否")


NOTICE_WORDS = (
    "@全体成员", "全体成员", "通知", "收取", "提交", "报到", "务必", "必须",
)
TECH_HINTS = ("喵", "哈哈", "笑死", "草", "跑分", "开播", "崩巴", "呜呜", "好耶",
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
    if _latin_ratio(text) > 0.7:
        score -= 2
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
CONFIDENCE_DENOM = 7.0  # 置信度 = score / 7，clamp 到 [0, 1]

# reason tag → (规则描述, 基础分)，给 cmd_explain 看
_TAG_META = {
    "行动词":    ("命中行动词", +3),
    "时间词":    ("命中时间词", +3),
    "指向我":    ("@我/帮我/你来", +2),
    "疑问":      ("语气是疑问", +1),
    "交易/引用": ("转账/红包/回复链", +1),
    "通知全体":  ("@全体成员", +4),
    "通知/要求": ("通知/收取/提交类措辞", +2),
    "疑似代码/技术闲聊": ("半角字母过半", -3),
    "语气词":    ("哈哈/笑死/666 等玩梗", -1),
    "重复字/玩梗": ("同字连续 ≥3", -1),
    "过短":      ("<8 字，多半寒暄", -2),
    "★重点":     ("特别关心来源", 0),   # 不是规则分，是展示标记
}


def tag_meta(tag: str) -> tuple:
    """把 reason tag 翻译成 (描述, 基础分)。
    tag 可能带参数（如 '行动词:记得/安排'），冒号前是 tag 名。"""
    base = tag.split(":", 1)[0].strip()
    meta = _TAG_META.get(base) or _TAG_META.get(tag.strip())
    if meta:
        return meta
    return (tag, 0)  # 未知 tag，给 0 分


def _hits(text: str, words: Iterable[str]) -> List[str]:
    return [w for w in words if w in text]


def _todo_text(content: str, limit: int = 200) -> str:
    """把消息裁成一条看得懂的待办文本。

    硬截前 200 字常把「三点前把材料交给老王」这类关键句砍在边界外，
    留下开头一整段无关铺垫。这里优先取包含动作词/时间词的句子。
    """
    text = (content or "").strip()
    if not text:
        return ""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    # 按句号/换行切句，挑第一句同时含动作或时间词的；都没有就退回前 limit 字
    parts = [p.strip() for p in re.split(r"[。！？!?\n]", flat) if p.strip()]
    marker = ACTION_WORDS + TIME_WORDS
    for part in parts:
        if any(w in part for w in marker) and len(part) <= limit:
            return part
    for part in parts:
        if any(w in part for w in marker):
            return part[:limit]
    return flat[:limit]


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
                  weights_map: dict = None,
                  todo_threshold: int = TODO_THRESHOLD,
                  weak_threshold: int = WEAK_THRESHOLD,
                  decisions: list = None) -> List[TodoItem]:
    """从消息流里抽取出待办，返回按时间排序的结果。

    todo_threshold / weak_threshold 默认取模块常量，但调用方（CLI / 看板）
    应从配置里传 todo_threshold / pending_threshold 进来——否则用户在
    c2t.toml 里改阈值完全不生效。

    decisions 可选出参：若传入一个 list，每条消息（含被过滤掉的）的判定结果
    会以 dict 形式 append 进去，供决策日志落库。不影响返回值。
    """
    todos: List[TodoItem] = []
    for msg in messages:
        kind = classify(msg)
        if kind in ("redpacket", "transfer"):
            # 红包/转账不是「要做的事」，但值得立刻提醒：生成一条高置信度条目，
            # 看板会据此弹提示，点完成后就不再打扰。
            body = (msg.content or "").strip() or "（无文字）"
            if kind == "redpacket":
                emoji, label, noun = "🧧", "红包提醒", "发的红包"
            else:
                emoji, label, noun = "💸", "转账提醒", "的转账"
            todos.append(
                TodoItem(
                    text=f"{emoji} {msg.sender_name} {noun}：{body}"[:200],
                    session=session,
                    sender=msg.sender_name,
                    message_ts=msg.timestamp,
                    reason=label,
                    confidence=0.95,
                    status="pending",
                    chat_id=msg.chat_id or "",
                )
            )
            if decisions is not None:
                decisions.append({
                    "ts": msg.timestamp, "sender_id": msg.sender_id,
                    "sender_name": msg.sender_name,
                    "content_hash": msg.content_hash,
                    "outcome": "pending", "score": 0,
                    "threshold": todo_threshold, "reason": label,
                })
            continue
        if kind == "risk":
            if decisions is not None:
                decisions.append({
                    "ts": msg.timestamp, "sender_id": msg.sender_id,
                    "sender_name": msg.sender_name,
                    "content_hash": msg.content_hash,
                    "outcome": "noise", "score": 0,
                    "threshold": todo_threshold, "reason": "risk 类型",
                })
            continue
        score, reasons = score_message(msg, weights_map)
        if decisions is not None:
            if score >= todo_threshold:
                outcome = "pending"
            elif score >= weak_threshold:
                outcome = "unconfirmed"
            elif score > 0:
                outcome = "filtered"
            else:
                outcome = "noise"
            decisions.append({
                "ts": msg.timestamp, "sender_id": msg.sender_id,
                "sender_name": msg.sender_name,
                "content_hash": msg.content_hash,
                "outcome": outcome, "score": score,
                "threshold": todo_threshold,
                "reason": "; ".join(reasons),
            })
        if score < weak_threshold:
            continue
        todos.append(
            TodoItem(
                text=_todo_text(msg.content),
                session=session,
                sender=msg.sender_name,
                message_ts=msg.timestamp,
                reason=("★重点 · " if getattr(msg, "star", False) else "") + "; ".join(reasons),
                confidence=round(min(max(score, 0) / 7.0, 1.0), 2),
                status="pending" if score >= todo_threshold else "unconfirmed",
                chat_id=msg.chat_id or "",
            )
        )
    todos.sort(key=lambda t: t.message_ts)
    return todos
