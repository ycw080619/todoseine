"""QQ NT 40800 消息体（protobuf BLOB）解码。

正文没有公开的 proto 定义，这里不写死字段结构，而是实现一个通用的
protobuf wire-format 解析器：递归遍历 BLOB，把「像文本」的长度限定字段
按文档序收集起来，过滤噪声（QQ 号 / URL / token / 路径）后拼接成正文。

设计约定：
- 纯标准库，与核心包「零依赖」原则一致；
- 解不出文本时返回空串，由调用方退回启发式（如 export.py 的 longest_text）；
- 字段对应关系可能随 QQ 版本漂移， iter_text() 可输出原始候选用于调试。
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Iterator, List, Tuple

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5

_MAX_DEPTH = 8        # 防御病态嵌套
_MAX_CANDIDATES = 200  # 防御超大 BLOB


@dataclass
class Field:
    """一个解析出的 protobuf 字段。wire == WIRE_LEN 时 value 为 bytes，否则为 int。"""

    number: int
    wire: int
    value: object


# --------------------------------------------------------------------------
# wire-format 解析
# --------------------------------------------------------------------------
def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    for shift in range(0, 70, 7):
        if pos >= len(buf):
            raise ValueError("varint 截断")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
    raise ValueError("varint 超长")


def parse_fields(buf: bytes) -> List[Field]:
    """把一段字节解析为字段列表；格式不符时抛 ValueError。"""
    fields: List[Field] = []
    pos, n = 0, len(buf)
    while pos < n:
        tag, pos = _read_varint(buf, pos)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("field number 0 非法")
        if wire == WIRE_VARINT:
            value, pos = _read_varint(buf, pos)
        elif wire == WIRE_FIXED64:
            if pos + 8 > n:
                raise ValueError("fixed64 截断")
            value = struct.unpack_from("<Q", buf, pos)[0]
            pos += 8
        elif wire == WIRE_LEN:
            length, pos = _read_varint(buf, pos)
            if pos + length > n:
                raise ValueError("LEN 字段越界")
            value = bytes(buf[pos:pos + length])
            pos += length
        elif wire == WIRE_FIXED32:
            if pos + 4 > n:
                raise ValueError("fixed32 截断")
            value = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
        else:  # 3/4 为废弃的 group，QQ 消息里不出现
            raise ValueError(f"不支持的 wire type {wire}")
        fields.append(Field(number, wire, value))
    return fields


# --------------------------------------------------------------------------
# 文本候选判定
# --------------------------------------------------------------------------
_CJK = re.compile(r"[一-鿿㐀-䶿]")
_URL = re.compile(r"https?://|www\.", re.I)
_PATH = re.compile(r"[A-Za-z]:[\\/]|(?:/[A-Za-z0-9_.-]+){2,}")
_DIGITS = re.compile(r"\d{5,}")           # QQ 号 / uid
_HEX = re.compile(r"[0-9a-fA-F]{16,}")    # hex token / guid
_B64 = re.compile(r"[A-Za-z0-9+/=]{24,}")  # base64 token
_UID_TOKEN = re.compile(r"u_[0-9A-Za-z_-]{16,}")  # @昵称后粘连的 NT uid
# 裸域名（无 scheme，_URL 拦不到），常粘在卡片占位文本尾巴上
_DOMAIN = re.compile(r"(?:[a-z0-9-]+\.)+(?:com\.cn|com|net|cn|me)(?::\d+)?(?:/\S*)?", re.I)


def _is_textish(s: str) -> bool:
    if not s or "\x00" in s:
        return False
    printable = sum(1 for ch in s if ch.isprintable() or ch == "\t")
    return printable / len(s) >= 0.95


def _is_noise(s: str) -> bool:
    st = s.strip()
    if not st:
        return True
    if _URL.search(st) or _PATH.search(st):
        return True
    if _DIGITS.fullmatch(st):
        return True
    if _HEX.fullmatch(st) or _B64.fullmatch(st):
        return True
    # 无中文、无空格的长串大概率是 token
    if not _CJK.search(st) and len(st) > 30 and " " not in st:
        return True
    return False


# --------------------------------------------------------------------------
# 卡片消息：JSON（f47901 一类）与 XML（聊天记录合并转发等）
# --------------------------------------------------------------------------
_CARD_TEXT_KEYS = ("prompt", "title", "desc")
_XML_BRIEF = re.compile(r'\bbrief="([^"]+)"')
_XML_TITLE = re.compile(r'<title[^>]*\btitle="([^"]+)"')


def _card_text(s: str) -> str:
    """候选若是 JSON 卡片，返回展示用文本；否则返回空串。"""
    st = s.strip()
    if not st.startswith("{"):
        return ""
    import json

    try:
        data = json.loads(st)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    for key in _CARD_TEXT_KEYS:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _xml_card_text(s: str) -> str:
    """XML 卡片：优先 brief 属性（如 "[聊天记录]"），其次 title。"""
    m = _XML_BRIEF.search(s) or _XML_TITLE.search(s)
    return m.group(1).strip() if m else ""


# --------------------------------------------------------------------------
# 遍历与提取
# --------------------------------------------------------------------------
def _walk(buf: bytes, depth: int, out: List[Tuple[int, str]]) -> None:
    if depth > _MAX_DEPTH or len(out) >= _MAX_CANDIDATES:
        return
    try:
        fields = parse_fields(buf)
    except ValueError:
        return
    for f in fields:
        if f.wire != WIRE_LEN:
            continue
        raw = f.value
        assert isinstance(raw, bytes)
        try:
            s = raw.decode("utf-8")
        except UnicodeDecodeError:
            s = ""
        if s and _is_textish(s):
            # 先按嵌套消息试解：包装层的字节往往「碰巧可打印」（如 "9\n7@…"），
            # 直接当文本会带上残渣；解不出候选再按真文本收。
            sub: List[Tuple[int, str]] = []
            _walk(raw, depth + 1, sub)
            if sub:
                out.extend(sub)
            else:
                out.append((depth, s))
        else:
            _walk(raw, depth + 1, out)


def iter_text(blob: bytes) -> Iterator[str]:
    """按文档序产出正文候选（剥 uid token、解 JSON 卡片、过滤噪声、去相邻重复）。"""
    out: List[Tuple[int, str]] = []
    _walk(bytes(blob), 0, out)
    prev = None
    for _, s in out:
        st = s.strip()
        if st.startswith("{"):
            # JSON 卡片：取展示字段；没有展示字段的是协议数据，跳过
            card = _card_text(s)
            if not card:
                continue
            s = card
        elif st.startswith("<?xml") or st.startswith("<msg"):
            card = _xml_card_text(s)
            if not card:
                continue
            s = card
        else:
            stripped = _UID_TOKEN.sub("", s)
            stripped = _DOMAIN.sub("", stripped)
            if stripped != s:  # 剥过 uid/域名才修边，保留正常尾随空格（如 "@全体成员 "）
                s = stripped.strip()
        if not s or s == prev or _is_noise(s):
            continue
        prev = s
        yield s


def looks_like_protobuf(blob: bytes) -> bool:
    """是否能按 protobuf 解析（供调用方决定是否回退启发式）。

    能解析但提不到文本 = 图片/媒体等结构化消息，不应退回启发式捡垃圾；
    解析失败 = 未知格式，可退回。
    """
    if not blob:
        return False
    try:
        return bool(parse_fields(bytes(blob)))
    except ValueError:
        return False


def decode_message_text(blob: bytes) -> str:
    """从 40800 BLOB 提取消息正文；提不到可靠文本时返回空串。"""
    if not blob:
        return ""
    text = "".join(iter_text(blob)).strip()
    return text if len(text) >= 2 else ""
