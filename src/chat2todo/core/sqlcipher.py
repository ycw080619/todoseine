"""SQLCipher 页面解密（可选，依赖 cryptography）。

之所以单独成模块：核心包保持零依赖，只有真正要读 QQ 数据库的人才需要装
`pip install chat2todo[db]`；没装时这里会抛 ImportError，由调用方降级。

参数来自社区验证过的 PRAGMA（顺序不能变）：
    cipher_page_size = 4096
    key = '<16 位 ASCII 口令>'
    kdf_iter = 4000
    cipher_hmac_algorithm = HMAC_SHA1
    cipher_kdf_algorithm = PBKDF2_HMAC_SHA512
"""

from __future__ import annotations

import hashlib
import struct
from typing import Optional, Tuple

HEADER_SIZE = 1024
PAGE_SIZE = 4096
DEFAULT_ITER = 4000
DEFAULT_DIGEST = "sha512"
SQLITE_MAGIC = b"SQLite format 3\x00"


def _aes_cbc(key: bytes, iv: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    out = dec.update(data) + dec.finalize()
    return out


def derive_keys(passphrase: str, salt: bytes, iterations: int = DEFAULT_ITER,
                digest: str = DEFAULT_DIGEST) -> Tuple[bytes, bytes]:
    """返回 (AES key, HMAC key)。SQLCipher 用一次 PBKDF2 产出两把 32 字节密钥。"""
    okm = hashlib.pbkdf2_hmac(digest, passphrase.encode("utf-8"), salt, iterations, 64)
    return okm[:32], okm[32:]


def try_layout(passphrase: str, header: bytes, page: bytes) -> Optional[bytes]:
    """按若干候选布局解第一页，返回明文首页（成功时以 SQLite magic 开头）。"""
    salt = header[1024:1040]
    iv = page[:16]
    for iterations in (DEFAULT_ITER, 256000, 64000):
        for digest in ("sha512", "sha1"):
            try:
                aes_key, _ = derive_keys(passphrase, salt, iterations, digest)
            except Exception:
                continue
            for reserve in (20, 32, 48):
                ct = page[16:len(page) - reserve]
                if len(ct) < 16 or len(ct) % 16:
                    continue
                try:
                    pt = _aes_cbc(aes_key, iv, ct)
                except Exception:
                    continue
                if pt.startswith(SQLITE_MAGIC):
                    return pt + b"\x00" * reserve
    return None


def decrypt_file(passphrase: str, src: str, dst: str) -> Tuple[bool, str]:
    """把 QQ 加密库解成普通 SQLite 文件。返回 (成功?, 说明)。"""
    try:
        with open(src, "rb") as fh:
            header = fh.read(HEADER_SIZE)
            first_page = fh.read(PAGE_SIZE)
            if len(first_page) < PAGE_SIZE:
                return False, "文件太短，不像完整数据库"
            plain_first = try_layout(passphrase, header, first_page)
            if plain_first is None:
                return False, "首页解不开：口令或 KDF 参数不匹配"
            with open(dst, "wb") as out:
                out.write(plain_first)
                salt = header[1024:1040]
                aes_key, _ = derive_keys(passphrase, salt)
                while True:
                    page = fh.read(PAGE_SIZE)
                    if len(page) < PAGE_SIZE:
                        break
                    iv = page[:16]
                    reserve = PAGE_SIZE - 16 - len(plain_first) + 16
                    ct = page[16:len(page) - 20]
                    out.write(_aes_cbc(aes_key, iv, ct) + b"\x00" * 20)
        return True, "解密完成"
    except ImportError:
        return False, "缺少 cryptography：pip install chat2todo[db]"
    except Exception as exc:  # noqa: BLE001
        return False, f"解密出错：{exc}"
