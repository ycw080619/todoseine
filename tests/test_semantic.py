"""vector_pass 主流程接入测试（假 Embedder，不联网）。"""

from __future__ import annotations

from chat2todo.models import TodoItem
from chat2todo.semantic import vector_pass


class FakeEmbedder:
    """按文本查表回向量；查不到的给单位向量。"""

    def __init__(self, table=None, fail=False):
        self.table = table or {}
        self.fail = fail
        self.available = True

    def embed(self, texts):
        if self.fail:
            return []
        return [self.table.get(t, [1.0, 0.0]) for t in texts]


def _todo(text, conf=0.8):
    return TodoItem(text=text, session="s", sender="u", message_ts=1, confidence=conf)


CFG = {"vector": {"enabled": True, "sim_threshold": 0.9, "ann_threshold": 0.8}}


def test_disabled_returns_unchanged():
    todos = [_todo("a"), _todo("b")]
    out, note = vector_pass(todos, {"vector": {"enabled": False}}, FakeEmbedder())
    assert out is todos and note == ""


def test_embed_failure_silent_fallback():
    todos = [_todo("a"), _todo("b")]
    out, note = vector_pass(todos, CFG, FakeEmbedder(fail=True))
    assert out == todos and note == ""


def test_semantic_dedupe_keeps_highest_confidence():
    from chat2todo.core.vector import ANNOUNCEMENT_SEEDS

    table = {s: [0.0, 0.0, 1.0] for s in ANNOUNCEMENT_SEEDS}  # 种子与待办正交
    table.update({
        "改天把材料发我": [1.0, 0.0, 0.0],
        "材料改天发我一下": [0.99, 0.01, 0.0],
        "今晚吃什么": [0.0, 1.0, 0.0],
    })
    todos = [_todo("改天把材料发我", conf=0.6),
             _todo("材料改天发我一下", conf=0.9),
             _todo("今晚吃什么", conf=0.7)]
    out, note = vector_pass(todos, CFG, FakeEmbedder(table))
    texts = [t.text for t in out]
    assert "材料改天发我一下" in texts          # 置信度高的留下
    assert "改天把材料发我" not in texts
    assert "今晚吃什么" in texts
    assert "合并重复 1" in note


def test_announcement_filtered():
    seed_vec = [1.0, 0.0]
    from chat2todo.core.vector import ANNOUNCEMENT_SEEDS

    table = {s: seed_vec for s in ANNOUNCEMENT_SEEDS}
    table["各位请注意，本周课程调整如下，望周知"] = [0.99, 0.01]   # 近似公告
    table["你明天把报名表交给我"] = [0.0, 1.0]
    todos = [_todo("各位请注意，本周课程调整如下，望周知"),
             _todo("你明天把报名表交给我")]
    out, note = vector_pass(todos, CFG, FakeEmbedder(table))
    assert [t.text for t in out] == ["你明天把报名表交给我"]
    assert "过滤公告 1" in note


def test_single_todo_skipped():
    todos = [_todo("a")]
    out, note = vector_pass(todos, CFG, FakeEmbedder())
    assert out == todos and note == ""
