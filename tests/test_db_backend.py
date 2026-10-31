"""内置后端端到端：用 sqlcipher3 自建一个「QQ 同款参数」的加密库，验证
BuiltinBackend 能快照、剥头、解密、解 protobuf、回出行数据。

没有 sqlcipher3 的环境自动跳过（它是可选依赖）。
"""

from __future__ import annotations

import os

import pytest

sqlcipher3 = pytest.importorskip("sqlcipher3", reason="可选依赖 sqlcipher3 未安装")

from todoseine.sources.db import (  # noqa: E402 - 需先 importorskip(sqlcipher3)
    HEADER_SIZE,
    MSG_TABLES,
    PRAGMA_BEFORE_KEY,
    SQLCIPHER_PRAGMAS,
    BuiltinBackend,
    inspect,
)

KEY = "0123456789abcdef"


def _varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _flen(no: int, payload: bytes) -> bytes:
    return _varint(no << 3 | 2) + _varint(len(payload)) + payload


def _blob(text: str) -> bytes:
    return _flen(1, _flen(1, _flen(1, _flen(1, text.encode("utf-8")))))


def _make_qq_like_db(tmp_path) -> str:
    """按 QQ NT 布局造一个加密库：1024 字节自定义头 + SQLCipher 页。"""
    plain = tmp_path / "enc.db"
    conn = sqlcipher3.connect(str(plain))
    conn.execute(PRAGMA_BEFORE_KEY)
    conn.execute(f"PRAGMA key = '{KEY}'")
    for pragma in SQLCIPHER_PRAGMAS:
        conn.execute(pragma)
    conn.execute(
        f'CREATE TABLE "{MSG_TABLES[0]}" ('
        '"40001" INTEGER, "40033" INTEGER, "40021" INTEGER, "40027" INTEGER, '
        '"40050" INTEGER, "40093" TEXT, "40800" BLOB)'
    )
    conn.execute(
        f'INSERT INTO "{MSG_TABLES[0]}" VALUES (?, ?, ?, ?, ?, ?, ?)',
        (111, 222, 10001, 0, 1703001600, "小李", _blob("明天下午三点前把周报发我")),
    )
    conn.commit()
    conn.close()

    wrapped = tmp_path / "nt_msg.db"
    with open(wrapped, "wb") as out:
        out.write(b"SQLite header 3" + b"\x00" * (HEADER_SIZE - 15))
        with open(plain, "rb") as fh:
            out.write(fh.read())
    os.remove(plain)
    return str(wrapped)


def test_builtin_backend_end_to_end(tmp_path):
    db_path = _make_qq_like_db(tmp_path)
    info = inspect(db_path)
    assert info.encrypted is True

    rows = BuiltinBackend(key=KEY).fetch(info)
    assert len(rows) == 1
    assert rows[0]["content"] == "明天下午三点前把周报发我"
    assert rows[0]["sender_name"] == "小李"
    # 会话号必须取 40033（群表=群号/私聊=对方QQ），不是 40002(msgRandom)
    assert rows[0]["chat_id"] == "222"


def test_builtin_backend_wrong_key(tmp_path):
    from todoseine.errors import C2TError

    db_path = _make_qq_like_db(tmp_path)
    info = inspect(db_path)
    with pytest.raises(C2TError) as exc:
        BuiltinBackend(key="fedcba9876543210").fetch(info)
    assert exc.value.code == "E4003"


def test_builtin_backend_no_key_raises():
    from todoseine.errors import C2TError

    with pytest.raises(C2TError) as exc:
        BuiltinBackend().fetch(None)  # type: ignore[arg-type]
    assert exc.value.code == "E4002"


def _make_group_info_db(tmp_path, key: str = KEY) -> str:
    """在 nt_msg.db 旁边造一个加密的 group_info.db（60001=群号 66530=内部代码 60007=群名）。"""
    from todoseine.sources.db import GROUP_INFO_TABLE

    plain = tmp_path / "ginfo_plain.db"
    conn = sqlcipher3.connect(str(plain))
    conn.execute(PRAGMA_BEFORE_KEY)
    conn.execute(f"PRAGMA key = '{key}'")
    for pragma in SQLCIPHER_PRAGMAS:
        conn.execute(pragma)
    conn.execute(f'CREATE TABLE "{GROUP_INFO_TABLE}" '
                 '("60001" INTEGER, "66530" INTEGER, "60007" TEXT)')
    conn.execute(f'INSERT INTO "{GROUP_INFO_TABLE}" VALUES (?, ?, ?)',
                 (116316805, 123343757, "C#技术交流"))
    conn.execute(f'INSERT INTO "{GROUP_INFO_TABLE}" VALUES (?, ?, ?)',
                 (12345, 67890, ""))  # 空名要被跳过
    conn.commit()
    conn.close()

    wrapped = tmp_path / "group_info.db"
    with open(wrapped, "wb") as out:
        out.write(b"SQLite header 3" + b"\x00" * (HEADER_SIZE - 15))
        with open(plain, "rb") as fh:
            out.write(fh.read())
    os.remove(plain)
    return str(wrapped)


def test_fetch_group_names(tmp_path, monkeypatch):
    """端到端：加密的 group_info.db → 群号与内部代码都能查到群名；空名跳过。"""
    from todoseine.sources import db as src_db

    db_path = _make_qq_like_db(tmp_path)
    _make_group_info_db(tmp_path)
    monkeypatch.setattr(src_db, "locate",
                        lambda root=None: __import__("pathlib").Path(db_path))
    monkeypatch.setattr(src_db, "resolve_key", lambda cfg: KEY)

    names = src_db.fetch_group_names({})
    assert names == {"116316805": "C#技术交流", "123343757": "C#技术交流"}


def test_fetch_group_names_degrades_silently(tmp_path, monkeypatch):
    """没有 group_info.db / 没有密钥时返回空 dict，不抛异常。"""
    from todoseine.sources import db as src_db

    db_path = _make_qq_like_db(tmp_path)  # 旁边不放 group_info.db
    monkeypatch.setattr(src_db, "locate",
                        lambda root=None: __import__("pathlib").Path(db_path))
    monkeypatch.setattr(src_db, "resolve_key", lambda cfg: KEY)
    assert src_db.fetch_group_names({}) == {}

    monkeypatch.setattr(src_db, "resolve_key", lambda cfg: "")
    _make_group_info_db(tmp_path)
    assert src_db.fetch_group_names({}) == {}
