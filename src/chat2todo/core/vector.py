"""本地向量语义层（零第三方依赖）。

用途：
  1. 语义去重 —— 同一件事被不同人转发，措辞不同但意思一样，靠余弦相似度合并；
  2. 语义过滤 —— 用几条"群公告/通知"种子句做最近邻判别，挡掉公告类噪声；
  3. 后续分类 —— 任何要"按意思归类"的需求，都可以在这层上做（各分类存几条种子句即可）。

后端默认走本机 Ollama 的 `/api/embed`（bge-m3，1024 维）；
不可用时全部方法返回空结果，调用方自己退回规则，绝不阻塞主流程。
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class Embedder:
    """把文本变成向量；批量、分块、失败静默。"""

    def __init__(self, base_url: str = "http://127.0.0.1:11434",
                 model: str = "bge-m3", timeout: int = 30,
                 batch: int = 24) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.batch = max(1, batch)

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.model)

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        out: List[List[float]] = []
        for i in range(0, len(texts), self.batch):
            chunk = list(texts[i:i + self.batch])
            vecs = self._call(chunk)
            if vecs is None:                     # 任何失败：整体放弃，调用方退回规则
                return []
            out.extend(vecs)
        return out

    def _call(self, texts: List[str]) -> Optional[List[List[float]]]:
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/embed", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            vecs = data.get("embeddings") or []
            if len(vecs) != len(texts):
                return None
            return vecs
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return None


def cluster(items: Sequence[Tuple[str, Sequence[float]]],
            threshold: float = 0.92) -> List[List[int]]:
    """贪心聚类：返回若干组的索引集合。items = [(text, vec)]。"""
    groups: List[List[int]] = []
    centroids: List[Sequence[float]] = []
    for idx, (_text, vec) in enumerate(items):
        placed = False
        for gi, cent in enumerate(centroids):
            if cosine(vec, cent) >= threshold:
                groups[gi].append(idx)
                placed = True
                break
        if not placed:
            groups.append([idx])
            centroids.append(vec)
    return groups


def best_match(vec: Sequence[float], seeds: Sequence[Sequence[float]]) -> float:
    """与一组种子向量的最高相似度。"""
    return max((cosine(vec, s) for s in seeds), default=0.0)


class VectorStore:
    """极简向量库：内存 + JSON 落盘，够几百到几万条用。"""

    def __init__(self) -> None:
        self.vectors: Dict[str, List[float]] = {}
        self.meta: Dict[str, dict] = {}

    def add(self, key: str, vec: Sequence[float], meta: Optional[dict] = None) -> None:
        self.vectors[key] = list(vec)
        self.meta[key] = meta or {}

    def search(self, vec: Sequence[float], k: int = 5) -> List[Tuple[str, float]]:
        scored = [(key, cosine(vec, v)) for key, v in self.vectors.items()]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"vectors": self.vectors, "meta": self.meta}, fh, ensure_ascii=False)

    def load(self, path: str) -> "VectorStore":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.vectors = {k: list(v) for k, v in data.get("vectors", {}).items()}
        self.meta = data.get("meta", {})
        return self


# 群公告/通知的代表句：只用来做最近邻判别，不参与生成
ANNOUNCEMENT_SEEDS = [
    "通知：请各位同学注意，本周课程调整如下",
    "群公告：请大家及时查看群文件并互相转告",
    "各位请注意，现将有关事项通知如下",
    "请大家知悉，相关安排如有变动另行通知",
    "全体成员请注意，本次会议安排如下",
]

# 真正"要你去做"的句子：用来给候选句做正例参考
TODO_SEEDS = [
    "你明天把材料交给我",
    "麻烦你确认一下这个方案",
    "记得周五前提交报名表",
]
