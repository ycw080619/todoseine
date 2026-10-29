"""从聊天文本里解析截止时间。

只覆盖中文聊天里最常见的说法，解析不出来就返回 None——宁可没有，也不要瞎猜。
支持：
    10月28号 / 10月28日 22:00前
    明天下午三点前 / 今晚19:20 / 后天中午
    周二 / 下周三 / 本周五
    只有时间没有日期时按「今天，如果已过则明天」处理
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

_REL = {
    "今天": 0, "今晚": 0, "今日": 0,
    "明天": 1, "明晚": 1, "明日": 1,
    "后天": 2, "大后天": 3,
}
_WEEK = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_PERIOD = {"凌晨": 0, "早上": 0, "上午": 0, "中午": 12, "下午": 12, "傍晚": 12, "晚上": 12, "今晚": 12}

_MD = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_WEEKDAY = re.compile(r"(下?)\s*(?:周|星期)\s*([一二三四五六日天])")
_HHMM = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
_HOUR = re.compile(r"(凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*(\d{1,2})\s*[点时](半)?")
_CN_HOUR = re.compile(r"(凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*([一二三四五六七八九十]{1,3})\s*[点时](半)?")
_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
       "九": 9, "十": 10, "十一": 11, "十二": 12}


def _cn_to_int(s: str) -> Optional[int]:
    return _CN.get(s)


def parse(text: str, now: Optional[datetime] = None) -> Optional[int]:
    """返回截止时间的 Unix 时间戳；无法判断时返回 None。"""
    if not text:
        return None
    now = now or datetime.now()

    date: Optional[datetime] = None
    if (m := _MD.search(text)):
        month, day = int(m.group(1)), int(m.group(2))
        year = now.year
        try:
            cand = now.replace(month=month, day=day, hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            return None
        if cand < now - timedelta(days=180):      # 明显是明年的事
            cand = cand.replace(year=year + 1)
        date = cand
    else:
        for word, delta in _REL.items():
            if word in text:
                date = (now + timedelta(days=delta)).replace(hour=0, minute=0, second=0, microsecond=0)
                break
    if date is None and (m := _WEEKDAY.search(text)):
        target = _WEEK[m.group(2)]
        ahead = (target - now.weekday()) % 7
        if m.group(1) == "下" or ahead == 0:
            ahead += 7 if m.group(1) == "下" else 0
        date = (now + timedelta(days=ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
    if date is None:
        # 只有时间（如「19点前到」）：按今天算，已过则明天
        if not (_HHMM.search(text) or _HOUR.search(text)):
            return None
        date = now.replace(hour=0, minute=0, second=0, microsecond=0)

    hour = minute = None
    if (m := _HHMM.search(text)):
        hour, minute = int(m.group(1)), int(m.group(2))
    elif (m := _HOUR.search(text)):
        period, hh, half = m.group(1), int(m.group(2)), m.group(3)
        if period in _PERIOD and period != "中午":
            hour = hh + _PERIOD[period] if hh < 12 else hh
        elif period == "中午":
            hour = 12 if hh >= 12 else hh
        else:
            hour = hh
        if 0 <= hour <= 23:
            minute = 30 if half else 0
        else:
            hour = minute = None
    elif (m := _CN_HOUR.search(text)):
        period, cn, half = m.group(1), _cn_to_int(m.group(2)), m.group(3)
        if cn is None:
            return None
        hh = cn
        if period in _PERIOD and period != "中午":
            hour = hh + _PERIOD[period] if hh < 12 else hh
        elif period == "中午":
            hour = 12 if hh >= 12 else hh
        else:
            hour = hh
        minute = 30 if half else 0

    if hour is None:
        result = date.replace(hour=23, minute=59)
    else:
        result = date.replace(hour=hour, minute=minute or 0)
    if result < now and date.date() == now.date():
        # 只有时间的场景：今天的时间已过 → 顺延到明天
        result += timedelta(days=1)
    if result < now - timedelta(days=1):
        return None                                # 明显是过去的旧消息，别当待办截止
    return int(result.timestamp())


def label(ts: Optional[int], now: Optional[datetime] = None) -> str:
    """把时间戳变成看板上的一小段文字：今天22:00 / 明天 / 3天后 / 已过期。"""
    if not ts:
        return ""
    now = now or datetime.now()
    due = datetime.fromtimestamp(ts)
    days = (due.date() - now.date()).days
    clock = due.strftime("%H:%M")
    if ts < now.timestamp():
        return "已过期"
    if days == 0:
        return f"今天{clock}"
    if days == 1:
        return f"明天{clock}"
    if days <= 7:
        return f"{days}天后"
    return due.strftime("%m-%d")


def urgency(ts: Optional[int], now: Optional[datetime] = None) -> int:
    """0=无期限 1=宽松 2=紧急（24 小时内）。"""
    if not ts:
        return 0
    now = now or datetime.now()
    return 2 if ts - now.timestamp() <= 86400 else 1
