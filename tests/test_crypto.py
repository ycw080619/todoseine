"""加密层测试：AES 正确性 + SQLCipher 首页校验。

AES 用 FIPS-197 标准向量自检；密钥校验只测「错误口令必须返回 False」，
真实口令不在测试范围内（也不可能在内）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from chat2todo.core.crypto import (  # noqa: E402
    cbc_decrypt_first_block,
    decrypt_block,
    derive_key,
    verify_sqlcipher_key,
)


class TestAES(unittest.TestCase):
    def test_fips197_vector(self) -> None:
        """FIPS-197 附录 C 的 AES-256 解密向量。"""
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
        ct = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")
        pt = decrypt_block(key, ct)
        self.assertEqual(pt.hex(), "00112233445566778899aabbccddeeff")

    def test_derive_key_is_deterministic(self) -> None:
        salt = b"0123456789abcdef"
        a = derive_key(b"pw", salt, iterations=1000)
        b = derive_key(b"pw", salt, iterations=1000)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32)
        self.assertNotEqual(derive_key(b"pw", salt, iterations=1001), a)


class TestVerify(unittest.TestCase):
    def _make_db(self, tmp: str) -> str:
        path = os.path.join(tmp, "nt_msg.db")
        with open(path, "wb") as fh:
            fh.write(b"SQLite header 3\x00".ljust(1024, b"\x00"))
            fh.write(b"\x01" * 16)   # salt
            fh.write(b"\x02" * 16)   # iv
            fh.write(b"\x03" * 64)   # ciphertext
        return path

    def test_wrong_key_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_db(tmp)
            self.assertFalse(verify_sqlcipher_key("definitely-not-the-key", path))

    def test_truncated_file_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "short.db")
            with open(path, "wb") as fh:
                fh.write(b"\x00" * 100)
            self.assertFalse(verify_sqlcipher_key("x", path))

    def test_first_block_roundtrip_shape(self) -> None:
        """CBC 首块长度恒为 16 字节，且对同一输入稳定。"""
        key = bytes(range(32))
        iv = bytes(16)
        ct = bytes(16)
        a = cbc_decrypt_first_block(key, iv, ct)
        b = cbc_decrypt_first_block(key, iv, ct)
        self.assertEqual(len(a), 16)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
