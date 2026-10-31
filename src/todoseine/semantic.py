"""向量语义层的主流程接入（可选增强）。

规则抽取得到候选待办后，若本机 Ollama 可用，再做两道语义清洗：
  1. 语义去重 —— 措辞不同但同一件事的合并，保留置信度最高的一条；
  2. 公告过滤 —— 与群公告种子句过于相似的视为通知，而非「要你做的事」。

任何一步失败（Ollama 没起、模型没拉、网络抖动）都静默退回规则结果，
绝不阻塞主流程——与 AI 复核同一原则。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .core.vector import ANNOUNCEMENT_SEEDS, Embedder, cluster, cosine
from .models import TodoItem

VECTOR_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "base_url": "http://127.0.0.1:11434",
    "model": "bge-m3",
    "timeout": 15,
    "batch": 24,
    "sim_threshold": 0.92,   # 高于此相似度视为同一件事
    "ann_threshold": 0.80,   # 高于此相似度视为群公告
}


def vector_pass(todos: List[TodoItem], cfg: Dict[str, Any],
                embedder: Embedder = None) -> Tuple[List[TodoItem], str]:
    """返回 (清洗后的 todos, 说明文字)；不可用时原样返回、说明为空。"""
    vcfg = dict(VECTOR_DEFAULTS)
    vcfg.update(cfg.get("vector", {}) if isinstance(cfg, dict) else {})
    if not vcfg.get("enabled") or len(todos) < 2:
        return todos, ""

    emb = embedder or Embedder(
        base_url=str(vcfg.get("base_url") or ""),
        model=str(vcfg.get("model") or ""),
        timeout=int(vcfg.get("timeout") or 15),
        batch=int(vcfg.get("batch") or 24),
    )
    if not emb.available:
        return todos, ""

    texts = [t.text for t in todos]
    vecs = emb.embed(texts)
    if not vecs:
        return todos, ""  # Ollama 不在：静默退回规则结果

    notes = []

    # 1) 语义去重：同组留置信度最高的一条，位置保持原消息顺序
    sim_th = float(vcfg.get("sim_threshold") or 0.92)
    groups = cluster(list(zip(texts, vecs)), threshold=sim_th)
    kept_idx = sorted(max(g, key=lambda i: (todos[i].confidence, -i)) for g in groups)
    merged = len(todos) - len(kept_idx)
    if merged:
        todos = [todos[i] for i in kept_idx]
        vecs = [vecs[i] for i in kept_idx]
        notes.append(f"合并重复 {merged}")

    # 2) 公告过滤：与公告种子句的最高相似度超阈值即丢弃（复用去重后的向量）
    ann_th = float(vcfg.get("ann_threshold") or 0.80)
    seed_vecs = emb.embed(ANNOUNCEMENT_SEEDS)
    if seed_vecs:
        kept = [t for t, v in zip(todos, vecs)
                if max(cosine(v, s) for s in seed_vecs) < ann_th]
        dropped = len(todos) - len(kept)
        if dropped:
            todos = kept
            notes.append(f"过滤公告 {dropped}")

    note = f"（向量清洗：{'，'.join(notes)}）" if notes else ""
    return todos, note
