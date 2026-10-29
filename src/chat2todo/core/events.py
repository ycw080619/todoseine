"""跨群事件级合并：同一件事被转发到多个群时合并成一条待办。

纯规则实现，不依赖向量（Ollama 不在也能跑）。设计参考 qq-live-digest/events.py。

合并条件（保守，宁可漏合不可误合）：
  - action 归一后必须相同（或都为空）
  - deadline 解析结果必须相同（或都为空）
  - 对象文本 bigram 重叠：Jaccard ≥ 0.5 或共享 bigram ≥ 3
  - 锚点要求：action+deadline 都有 → 弱重叠即可；只有 action → 需共享 ≥3；
    两者都空 → 需共享 ≥5（几乎只合并原文高度相似的）
  - 必须来自不同 chat_id（同群内的重复由 dedupe/vector_pass 处理）
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..models import Message, TodoItem
from .deadline import parse as parse_deadline

# 动作词 → 归一化键。顺序即优先级，取第一个命中。
ACTION_MAP: List[Tuple[Tuple[str, ...], str]] = [
    (("报名", "签到", "打卡", "报个名"), "signup"),
    (("缴费", "交钱", "付款", "交费", "转账给"), "pay"),
    (("提交", "上交", "交一下", "交作业"), "submit"),
    (("考试", "测验", "期中", "期末", "月考"), "exam"),
    (("开会", "会议", "开会了", "开个会"), "meeting"),
    (("值班", "值日", "值周"), "duty"),
    (("填写", "填一下", "填表", "填个表"), "fill"),
    (("领取", "领一下", "领东西"), "claim"),
    (("彩排", "排练", "走台"), "rehearse"),
    (("比赛", "竞赛", "答辩"), "compete"),
]

# 这些词从对象文本里剔除，避免干扰 bigram 重叠计算
_STRIP_WORDS = (
    "记得", "别忘", "不要忘", "安排", "确认", "准备", "发我", "给我",
    "处理", "跟进", "回复", "检查", "核对", "务必", "需要", "整理",
    "今天", "明天", "后天", "本周", "下周", "周一", "周二", "周三",
    "周四", "周五", "周六", "周日", "上午", "下午", "晚上", "点前",
    "之前", "截止", "月底", "尽快", "今天内", "请", "大家", "各位",
    "通知", "公告", "@全体成员", "全体成员", "的", "了", "吗", "吧",
)


def _extract_action(text: str) -> Optional[str]:
    for words, key in ACTION_MAP:
        if any(w in text for w in words):
            return key
    return None


def _object_bigrams(text: str) -> Set[str]:
    """去掉动作词/时间词后的字符 bigram 集合。"""
    cleaned = text
    for w in _STRIP_WORDS:
        cleaned = cleaned.replace(w, "")
    cleaned = "".join(ch for ch in cleaned if not ch.isspace())
    if len(cleaned) < 2:
        return set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def extract_event_key(text: str) -> dict:
    """从一条消息文本提取事件键要素。

    返回 {"action": str|None, "deadline": int|None, "bigrams": set[str]}
    """
    return {
        "action": _extract_action(text),
        "deadline": parse_deadline(text),
        "bigrams": _object_bigrams(text),
    }


def _compatible(a: dict, b: dict) -> bool:
    """判断两个事件键是否指向同一件事。"""
    # action 必须相同（都为空也算相同，但会被锚点规则挡掉）
    if a["action"] != b["action"]:
        return False
    # deadline 必须相同（都为空也算相同）
    if a["deadline"] != b["deadline"]:
        return False
    # bigram 重叠
    shared = len(a["bigrams"] & b["bigrams"])
    jacc = _jaccard(a["bigrams"], b["bigrams"])
    has_action = a["action"] is not None
    has_deadline = a["deadline"] is not None
    if has_action and has_deadline:
        return jacc >= 0.5 or shared >= 3
    if has_action:
        return shared >= 3
    return shared >= 5   # 无锚点：几乎原文一样才合并


def merge_events(todos: List[TodoItem],
                 messages: Iterable[Message]) -> Tuple[List[TodoItem], str]:
    """跨群合并：把讲同一件事、来自不同群的待办合成一条。

    返回 (合并后的 todos, 说明文字)。合并后保留置信度最高的一条，
    reason 前加「[跨群合并: N群]」标记。
    """
    if len(todos) < 2:
        return todos, ""

    # 建 (ts, sender_name) -> message 查找表，取原文算事件键
    msg_by_key: Dict[Tuple[int, str], Message] = {}
    for m in messages:
        msg_by_key[(m.timestamp, m.sender_name or "")] = m

    # 每条 todo 的事件键
    keys: List[Optional[dict]] = []
    for t in todos:
        m = msg_by_key.get((t.message_ts, t.sender or ""))
        text = m.content if m else t.text
        keys.append(extract_event_key(text))

    # 贪心分组：按顺序找可合并的组
    groups: List[List[int]] = []
    for i in range(len(todos)):
        placed = False
        for g in groups:
            # 必须与组内「所有」成员都兼容才加入。
            # 用 any() 会形成链式合并（A≈B、B≈C 就把 A、C 也并掉），
            # 即便 A 与 C 本不是一件事，合法的待办会被合成一条永久丢失。
            if all(_compatible(keys[i], keys[j]) for j in g):
                g.append(i)
                placed = True
                break
        if not placed:
            groups.append([i])

    merged_count = 0
    result: List[TodoItem] = []
    for g in groups:
        if len(g) == 1:
            result.append(todos[g[0]])
            continue
        # 跨群才合并：组内 chat_id 去重后 > 1
        chat_ids = {todos[j].chat_id for j in g if todos[j].chat_id}
        if len(chat_ids) <= 1:
            result.extend(todos[j] for j in g)
            continue
        # 保留置信度最高的，reason 加合并标记
        keep = max(g, key=lambda j: (todos[j].confidence, -todos[j].message_ts))
        base = todos[keep]
        merged = TodoItem(
            text=base.text,
            session=base.session,
            sender=base.sender,
            message_ts=base.message_ts,
            reason=f"[跨群合并: {len(chat_ids)}群] {base.reason}",
            confidence=base.confidence,
            status=base.status,
            chat_id=base.chat_id,
        )
        result.append(merged)
        merged_count += len(g) - 1

    note = f"（跨群合并：{merged_count}）" if merged_count else ""
    return result, note
