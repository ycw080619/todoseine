"""pbbody：40800 protobuf BLOB 解码测试。

没有真实库可依赖，全部用手工构造的 wire-format BLOB 验证：
编码器只写测试需要的三个原语（varint / LEN / 嵌套消息）。
"""

from __future__ import annotations

from chat2todo.core.pbbody import decode_message_text, iter_text, parse_fields


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


def _fvar(no: int, v: int) -> bytes:
    return _varint(no << 3 | 0) + _varint(v)


def _text(s: str) -> bytes:
    return s.encode("utf-8")


# --------------------------------------------------------------------------
# parse_fields
# --------------------------------------------------------------------------
def test_parse_varint_and_len():
    blob = _fvar(1, 300) + _flen(2, b"ab")
    f = parse_fields(blob)
    assert [(x.number, x.wire, x.value) for x in f] == [(1, 0, 300), (2, 2, b"ab")]


def test_parse_rejects_garbage():
    for blob in (b"\x00", b"\x0b", b"\xff", b"\x12\x05ab"):
        try:
            parse_fields(blob)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应当拒绝：{blob!r}")


# --------------------------------------------------------------------------
# decode_message_text
# --------------------------------------------------------------------------
def test_nested_text_elems_joined_in_order():
    """两段文本元素（中间夹二进制元素）按文档序拼接。"""
    elem1 = _flen(1, _flen(1, _text("明天下午三点前")))
    elem_pic = _flen(2, bytes(range(1, 30)))  # 图片等元素：二进制，忽略
    elem2 = _flen(1, _flen(1, _text("把周报发我")))
    rich = _flen(1, elem1 + elem_pic + elem2)
    blob = _flen(1, rich) + _fvar(2, 1)
    assert decode_message_text(blob) == "明天下午三点前把周报发我"


def test_mention_text_kept():
    elem_at = _flen(1, _flen(1, _text("@全体成员 ")))
    elem_txt = _flen(1, _flen(1, _text("记得报名")))
    blob = _flen(1, _flen(1, elem_at + elem_txt))
    assert decode_message_text(blob) == "@全体成员 记得报名"


def test_noise_filtered():
    """QQ 号、URL、hex token 不进正文。"""
    blob = _flen(1, _flen(1, _text("231458000")))  # 纯数字 uid
    assert decode_message_text(blob) == ""

    blob = _flen(1, _flen(1, _text("9f86d081884c7d659a2feaa0c55ad015")))  # hex
    assert decode_message_text(blob) == ""

    elem_url = _flen(1, _flen(1, _text("https://example.com/download?x=1")))
    elem_txt = _flen(1, _flen(1, _text("周五前确认")))
    blob = _flen(1, _flen(1, elem_url + elem_txt))
    assert decode_message_text(blob) == "周五前确认"


def test_adjacent_duplicates_dropped():
    elem = _flen(1, _flen(1, _text("收到")))
    blob = _flen(1, _flen(1, elem + elem))
    assert decode_message_text(blob) == "收到"


def test_empty_and_garbage_return_empty():
    assert decode_message_text(b"") == ""
    assert decode_message_text(b"\xff\xfe\x00\x01" + "随机".encode("utf-8")) == ""
    assert decode_message_text(b"not a protobuf at all") == ""


def test_single_char_and_short_text_not_returned():
    blob = _flen(1, _flen(1, _text("好")))
    assert decode_message_text(blob) == ""


def test_iter_text_exposes_candidates_for_debug():
    elem1 = _flen(1, _flen(1, _text("第一条")))
    elem2 = _flen(1, _flen(1, _text("第二条")))
    blob = _flen(1, _flen(1, elem1 + elem2))
    assert list(iter_text(blob)) == ["第一条", "第二条"]


# --------------------------------------------------------------------------
# 真实数据调优（6000 条样本驱动）
# --------------------------------------------------------------------------
def test_uid_token_stripped_from_mention():
    """@昵称 后粘连的 NT uid（u_xxx）应剥掉，昵称保留。"""
    elem = _flen(1, _flen(1, _text("@震惊!大新闻!u_YqB0UBtOA7xJqVWLP9vBLw 看下这个")))
    blob = _flen(1, _flen(1, elem))
    assert decode_message_text(blob) == "@震惊!大新闻! 看下这个"


def test_json_card_prompt_extracted():
    """JSON 卡片消息提取 prompt 字段。"""
    import json as _json

    card = _json.dumps({"ver": "1.0.0.19", "prompt": "[分享] 群公告",
                        "meta": {"detail": {"url": "https://x.com"}}})
    blob = _flen(1, _flen(47901, _text(card)))
    assert decode_message_text(blob) == "[分享] 群公告"


def test_json_without_display_fields_yields_nothing():
    blob = _flen(1, _flen(47901, _text('{"ver":"1.0.0","a":"b"}')))
    assert decode_message_text(blob) == ""


def test_xml_card_brief_extracted():
    """XML 卡片（合并转发等）提取 brief 属性。"""
    xml = ("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
           '<msg serviceID="35" brief="[聊天记录] 3 条消息"><item/></msg>')
    blob = _flen(1, _flen(14, _text(xml)))
    assert decode_message_text(blob) == "[聊天记录] 3 条消息"


def test_xml_card_without_brief_skipped():
    blob = _flen(1, _flen(14, _text("<?xml version='1.0'?><msg><item/></msg>")))
    assert decode_message_text(blob) == ""


def test_bare_domain_stripped_from_card_text():
    """卡片占位文本尾巴上粘连的裸域名应剥掉。"""
    elem = _flen(1, _flen(1, _text("[动画表情]multimedia.nt.qq.com.cn")))
    blob = _flen(1, _flen(1, elem))
    assert decode_message_text(blob) == "[动画表情]"


def test_looks_like_protobuf_gates_heuristic_fallback():
    from chat2todo.core.pbbody import looks_like_protobuf

    # 媒体消息：结构合法但无文本 → True（调用方不应回退启发式）
    media = _flen(40800, _flen(45101, bytes(range(1, 200))))
    assert looks_like_protobuf(media) is True
    assert decode_message_text(media) == ""
    # 垃圾字节 → False（可回退）
    assert looks_like_protobuf(b"\xff\xfe\x01\x02\x03") is False
    assert looks_like_protobuf(b"") is False
