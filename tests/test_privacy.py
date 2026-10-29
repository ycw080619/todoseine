"""隐私脱敏测试：内置规则、优先级、白名单、展示开关、日志 filter。"""

from __future__ import annotations

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from chat2todo.core.privacy import (  # noqa: E402
    MASK_HINT,
    RedactingFilter,
    group_whitelisted,
    redact,
    redact_for_ai,
    redact_for_archive,
    redact_for_display,
)


def _cfg(**privacy) -> dict:
    base = {"enabled": True, "mask_logs": True, "board_show_original": False,
            "whitelist_groups": [], "custom_rules": []}
    base.update(privacy)
    return {"privacy": base}


class TestRedact(unittest.TestCase):
    def test_phone(self) -> None:
        out, hits = redact("电话 13812345678 找我")
        self.assertEqual(out, "电话 138****5678 找我")
        self.assertEqual([h.kind for h in hits], ["手机号"])

    def test_id_card_not_eaten_by_bank_card(self) -> None:
        out, hits = redact("身份证 110101199003071234")
        # 18 位身份证：前 6 后 4，说明身份证规则优先于 16-19 位银行卡
        self.assertEqual(out, "身份证 110101********1234")
        self.assertEqual([h.kind for h in hits], ["身份证"])

    def test_bank_card(self) -> None:
        out, _ = redact("卡号 6222020200112233445")
        self.assertTrue(out.endswith("3445"))
        self.assertIn("*", out)

    def test_email(self) -> None:
        out, _ = redact("发到 zhang.san@example.com")
        self.assertEqual(out, "发到 z***@example.com")

    def test_no_false_positive_on_short_digits(self) -> None:
        text = "编号 12345678，另一个 2026"
        out, hits = redact(text)
        self.assertEqual(out, text)
        self.assertEqual(hits, [])

    def test_custom_rule(self) -> None:
        out, hits = redact("工号 AB123456", ["工号::\\b[A-Z]{2}\\d{6}\\b"])
        self.assertEqual(out, "工号 ********")
        self.assertEqual([h.kind for h in hits], ["工号"])

    def test_bad_custom_rule_is_skipped(self) -> None:
        out, hits = redact("手机 13812345678", ["坏::[unclosed"])
        self.assertEqual(out, "手机 138****5678")
        self.assertEqual([h.kind for h in hits], ["手机号"])

    def test_multi_occurrence_offsets(self) -> None:
        out, hits = redact("13812345678 与 13987654321")
        self.assertEqual(out, "138****5678 与 139****4321")
        self.assertEqual(len(hits), 2)


class TestWhitelist(unittest.TestCase):
    def test_archive_skips_whitelisted_group(self) -> None:
        cfg = _cfg(whitelist_groups=["家人群"])
        text, hits = redact_for_archive("电话 13812345678", "家人群", cfg)
        self.assertEqual(text, "电话 13812345678")
        self.assertEqual(hits, [])

    def test_archive_masks_other_group(self) -> None:
        cfg = _cfg(whitelist_groups=["家人群"])
        text, _ = redact_for_archive("电话 13812345678", "工作群", cfg)
        self.assertIn("****", text)

    def test_ai_ignores_whitelist(self) -> None:
        cfg = _cfg(whitelist_groups=["家人群"])
        self.assertIn("****", redact_for_ai("电话 13812345678", cfg))

    def test_disabled_means_no_masking(self) -> None:
        cfg = _cfg(enabled=False)
        self.assertTrue(group_whitelisted("任意群", cfg))
        self.assertEqual(redact_for_ai("13812345678", cfg), "13812345678")


class TestDisplay(unittest.TestCase):
    def test_default_masks_and_marks(self) -> None:
        out = redact_for_display("电话 13812345678", _cfg(), "工作群")
        self.assertIn("****", out)
        self.assertTrue(out.endswith(MASK_HINT))

    def test_show_original_switch(self) -> None:
        out = redact_for_display("电话 13812345678", _cfg(board_show_original=True))
        self.assertEqual(out, "电话 13812345678")

    def test_display_whitelist(self) -> None:
        out = redact_for_display("电话 13812345678", _cfg(whitelist_groups=["家人群"]), "家人群")
        self.assertEqual(out, "电话 13812345678")


class TestAiRawSwitch(unittest.TestCase):
    """AI 明文是例外通道：默认关，开启后还能按群号收窄。"""

    def test_default_still_masks(self) -> None:
        cfg = _cfg(allow_raw_ai=False, raw_ai_groups=[])
        self.assertIn("****", redact_for_ai("电话 13812345678", cfg, "工作群"))

    def test_open_all_groups(self) -> None:
        cfg = _cfg(allow_raw_ai=True, raw_ai_groups=[])
        self.assertEqual(redact_for_ai("电话 13812345678", cfg, "工作群"),
                         "电话 13812345678")

    def test_open_but_group_limited(self) -> None:
        cfg = _cfg(allow_raw_ai=True, raw_ai_groups=["家人群"])
        self.assertEqual(redact_for_ai("电话 13812345678", cfg, "家人群"),
                         "电话 13812345678")
        # 不在明报名单里的群仍然强制脱敏
        self.assertIn("****", redact_for_ai("电话 13812345678", cfg, "工作群"))

    def test_open_without_session_masks(self) -> None:
        cfg = _cfg(allow_raw_ai=True, raw_ai_groups=["家人群"])
        self.assertIn("****", redact_for_ai("电话 13812345678", cfg))


class TestLogFilter(unittest.TestCase):
    def test_record_msg_redacted(self) -> None:
        record = logging.LogRecord("t", logging.INFO, __file__, 1,
                                   "用户 13812345678 登录", (), None)
        RedactingFilter(_cfg()).filter(record)
        self.assertIn("****", record.msg)
        self.assertNotIn("13812345678", record.msg)

    def test_record_args_redacted(self) -> None:
        record = logging.LogRecord("t", logging.INFO, __file__, 1,
                                   "手机 %s", ("13812345678",), None)
        RedactingFilter(_cfg()).filter(record)
        self.assertIn("****", record.args[0])

    def test_mask_logs_off(self) -> None:
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "13812345678", (), None)
        RedactingFilter(_cfg(mask_logs=False)).filter(record)
        self.assertEqual(record.msg, "13812345678")


if __name__ == "__main__":
    unittest.main()
