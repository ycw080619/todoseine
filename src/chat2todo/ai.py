"""AI 增强层（全部默认关闭，失败即静默退回规则结果）。

五件事，对应文档 8.1：
  1. 边界复核 review  —— 规则拿不准的，问一次是不是待办
  2. 同事实归并 merge —— 多条讲同一件事的，合成一条
  3. 说话人意图 intent —— 别人要求我做 vs 他自己要做
  4. 待办改写 rewrite —— 口语碎片改成可执行描述
  5. 口吻改写（扩展包，本模块只提供 rewrite 的通用入口）

三条底线：
  * 总开关 ai.enabled 关闭时，本模块的任何方法都不会发请求；
  * 只发送待判定的单条文本（调用方自己决定传什么），从不整库上传；
  * 任何异常都返回 None，由调用方保留规则结果，不阻塞导入。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

from .config import AI_DEFAULTS


class AI:
    def __init__(self, cfg: dict) -> None:
        ai = dict(AI_DEFAULTS)
        ai.update(cfg.get("ai") or {})
        self.cfg = ai
        self.enabled = bool(ai.get("enabled"))
        self.base_url = str(ai.get("base_url") or "").rstrip("/")
        self.api_key = str(ai.get("api_key") or "")
        import os

        self.api_key = self.api_key or os.environ.get("C2T_AI_KEY", "")
        self.model = str(ai.get("model") or "gpt-4o-mini")
        self.timeout = int(ai.get("timeout") or 15)
        self.calls = 0

    # --- 能力开关 -------------------------------------------------
    def on(self, feature: str) -> bool:
        return bool(self.enabled and self.cfg.get(feature))

    @property
    def usable(self) -> bool:
        return bool(self.enabled and self.base_url and self.api_key)

    # --- 底层调用 -------------------------------------------------
    def _chat(self, system: str, user: str, max_tokens: int = 256,
              timeout: Optional[int] = None) -> Optional[str]:
        if not self.usable:
            return None
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            self.calls += 1
            return body["choices"][0]["message"]["content"].strip()
        except (urllib.error.URLError, KeyError, IndexError, ValueError, OSError):
            return None

    # --- 五个环节 -------------------------------------------------
    def review(self, text: str) -> Optional[bool]:
        """是不是真的要我做？返回 None 表示无法判断，调用方保留原结论。"""
        out = self._chat(
            "你是待办判定器。只回一个字：是 或 否。"
            "判定标准：只要这句话在提醒、通知或要求读者去做某件事，就算“是”；"
            "面向全体的通知（如“大家记得报名”）也算；纯闲聊、纯陈述事实算“否”。",
            f"这句话是否包含需要读者去做的事：\n{text}",
            max_tokens=4,
        )
        if not out:
            return None
        return out.startswith("是")

    def intent(self, text: str) -> Optional[bool]:
        """True 表示「有需要我去做的事」，False 表示「只是说话人自己的事」。"""
        out = self._chat(
            "判断这句话里有没有需要读者（我）去做的事。"
            "面向全体的提醒、通知、要求也算（例如“大家记得报名”）。"
            "只有纯粹是说话人陈述自己要做什么时才回 B。只回 A 或 B。"
            "A=有我该做的事，B=只是说话人自己的事。",
            text,
            max_tokens=4,
        )
        if not out:
            return None
        return out.strip().upper().startswith("A")

    def rewrite(self, text: str) -> Optional[str]:
        out = self._chat(
            "把口语化的要求改写成一条可执行待办，格式：时间 + 动作 + 对象。"
            "保留原文信息，不要编造。只输出改写结果一行。",
            text,
            max_tokens=80,
        )
        return out.strip() if out else None

    def refine_ocr(self, text: str) -> Optional[str]:
        """把截图 OCR 出来的毛边文字整理干净：纠错、去噪、按行保留事实。"""
        if not text.strip():
            return None
        text = text[:4000]
        out = self._chat(
            "你是 OCR 文本整理器。输入是从截图里识别出来的文字，可能有错别字、"
            "断行错乱、无关噪声（时间戳、昵称、按钮文字）。请："
            "1) 修正明显的 OCR 错字；2) 去掉与内容无关的噪声；"
            "3) 整理成通顺、保留全部关键事实的分行文本。"
            "严禁编造原文没有的信息，宁可保留原样。只输出整理后的文本。",
            text,
            max_tokens=600,
            timeout=max(int(self.timeout), 120),   # OCR 文本长，单次放宽超时
        )
        return out.strip() if out else None

    def merge(self, texts: list[str]) -> Optional[list[str]]:
        """把讲同一件事的多条合并；返回合并后的列表。"""
        if len(texts) < 2:
            return None
        numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, 1))
        out = self._chat(
            "下面若干条待办里，有些其实在说同一件事。请把说同一件事的合并成一条，"
            "其余原样保留。只输出 JSON 数组，例如 [\"合并后的待办\"]。",
            numbered,
            max_tokens=300,
        )
        if not out:
            return None
        try:
            data = json.loads(out[out.find("["): out.rfind("]") + 1])
            return [str(x) for x in data] if isinstance(data, list) else None
        except (ValueError, IndexError):
            return None
