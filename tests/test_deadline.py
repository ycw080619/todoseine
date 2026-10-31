"""截止时间解析测试（用固定 now，避免依赖真实时间）。"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from todoseine.core.deadline import label, parse, urgency  # noqa: E402

NOW = datetime(2026, 10, 25, 9, 0, 0)  # 周日


class TestParse(unittest.TestCase):
    def test_month_day_with_hhmm(self) -> None:
        ts = parse("建议提交时间：10月28号晚上22:00前", NOW)
        self.assertIsNotNone(ts)
        dt = datetime.fromtimestamp(ts)
        self.assertEqual((dt.month, dt.day), (10, 28))

    def test_tomorrow_afternoon(self) -> None:
        ts = parse("明天下午三点前把周报发我", NOW)
        dt = datetime.fromtimestamp(ts)
        self.assertEqual((dt.month, dt.day), (10, 26))
        self.assertEqual(dt.hour, 15)

    def test_today_evening(self) -> None:
        ts = parse("今晚19:20 心理教育课", NOW)
        dt = datetime.fromtimestamp(ts)
        self.assertEqual((dt.day, dt.hour, dt.minute), (25, 19, 20))

    def test_only_clock_past_rolls_to_tomorrow(self) -> None:
        ts = parse("8点前到", NOW)          # 早上 9 点说 8 点，应当顺延
        self.assertIsNotNone(ts)
        self.assertGreaterEqual(datetime.fromtimestamp(ts).day, 26)

    def test_no_date_returns_none(self) -> None:
        self.assertIsNone(parse("大家记得报名下周的分享会", NOW.replace(day=25)))
        self.assertIsNone(parse("哈哈哈笑死", NOW))

    def test_weekday(self) -> None:
        ts = parse("周五前交材料", NOW)
        self.assertIsNotNone(ts)
        self.assertEqual(datetime.fromtimestamp(ts).weekday(), 4)


class TestLabel(unittest.TestCase):
    def test_labels(self) -> None:
        due_today = int(NOW.replace(hour=19, minute=20).timestamp())
        self.assertEqual(label(due_today, NOW), "今天19:20")
        due_tmr = int((NOW.replace(hour=12, minute=0) + __import__("datetime").timedelta(days=1)).timestamp())
        self.assertEqual(label(due_tmr, NOW), "明天12:00")
        self.assertEqual(label(None, NOW), "")
        past = int((NOW - __import__("datetime").timedelta(hours=5)).timestamp())
        self.assertEqual(label(past, NOW), "已过期")

    def test_urgency(self) -> None:
        self.assertEqual(urgency(None, NOW), 0)
        self.assertEqual(urgency(int(NOW.timestamp()) + 3600, NOW), 2)
        self.assertEqual(urgency(int(NOW.timestamp()) + 86400 * 3, NOW), 1)


if __name__ == "__main__":
    unittest.main()
