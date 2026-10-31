"""密钥管理（keybox / resolve_key）与内置后端行解码的测试。"""

from __future__ import annotations

import sys

import pytest

from todoseine.core import keybox
from todoseine.sources.db import _decode_records, resolve_key


# --------------------------------------------------------------------------
# keybox：DPAPI 往返
# --------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 仅 Windows")
def test_keybox_roundtrip(tmp_path):
    f = tmp_path / "dbkey.dat"
    keybox.save_key("0123456789abcdef", str(f))
    assert f.read_bytes() != b"0123456789abcdef"  # 落盘的不是明文
    assert keybox.load_key(str(f)) == "0123456789abcdef"
    assert keybox.delete_key(str(f)) is True
    assert keybox.load_key(str(f)) == ""


def test_keybox_missing_file(tmp_path):
    assert keybox.load_key(str(tmp_path / "none.dat")) == ""


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 仅 Windows")
def test_keybox_garbage_file_returns_empty(tmp_path):
    f = tmp_path / "dbkey.dat"
    f.write_bytes(b"\xff\xfe not dpapi not hex")
    assert keybox.load_key(str(f)) == ""


# --------------------------------------------------------------------------
# resolve_key 优先级：配置 > 环境变量 > key_file > DPAPI 密库
# --------------------------------------------------------------------------
def test_resolve_key_config_first(monkeypatch, tmp_path):
    monkeypatch.setenv("C2T_DB_KEY", "env-key")
    kf = tmp_path / "k.txt"
    kf.write_text("file-key", encoding="utf-8")
    assert resolve_key({"key": "cfg-key", "key_file": str(kf)}) == "cfg-key"


def test_resolve_key_env_second(monkeypatch, tmp_path):
    monkeypatch.setenv("C2T_DB_KEY", "env-key")
    kf = tmp_path / "k.txt"
    kf.write_text("file-key", encoding="utf-8")
    assert resolve_key({"key": "", "key_file": str(kf)}) == "env-key"


def test_resolve_key_file_third(monkeypatch, tmp_path):
    monkeypatch.delenv("C2T_DB_KEY", raising=False)
    kf = tmp_path / "k.txt"
    kf.write_text("file-key\n", encoding="utf-8")
    assert resolve_key({"key": "", "key_file": str(kf)}) == "file-key"


def test_resolve_key_keybox_last(monkeypatch):
    monkeypatch.delenv("C2T_DB_KEY", raising=False)
    monkeypatch.setattr(keybox, "load_key", lambda *a, **k: "box-key")
    assert resolve_key({"key": "", "key_file": ""}) == "box-key"


# --------------------------------------------------------------------------
# _decode_records：40800 protobuf 解码 + 字段兜底
# --------------------------------------------------------------------------
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
    elem = _flen(1, _flen(1, text.encode("utf-8")))
    return _flen(1, _flen(1, elem))


def test_decode_records_happy_path():
    recs = [(111, 222, "10001", "", 1703001600, "小李", _blob("明天三点前把表发我"))]
    rows = _decode_records(recs)
    assert len(rows) == 1
    row = rows[0]
    assert row["content"] == "明天三点前把表发我"
    assert row["sender"] == "10001"
    assert row["sender_name"] == "小李"
    assert row["msg_id"] == "111" and row["chat_id"] == "222"


def test_decode_records_sender_fallback_and_bad_nick():
    # 40027 为空退回 40021；昵称是纯数字时退回 QQ 号
    recs = [(1, 2, "10001", None, 1703001600, "10001", _blob("记得报名"))]
    assert _decode_records(recs)[0]["sender_name"] == "10001"


def test_decode_records_filters_short_and_empty():
    recs = [
        (1, 2, "1", "", 1703001600, "a", _blob("好")),      # 太短
        (2, 2, "1", "", 1703001600, "a", b"\xff\xfe"),       # 解不出
        (3, 2, "1", "", 1703001600, "a", b""),               # 空
    ]
    assert _decode_records(recs) == []
