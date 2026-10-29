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

import ipaddress
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Callable, Optional
from urllib.parse import urlparse

from .config import AI_DEFAULTS, _as_bool
from .core.privacy import redact_for_ai

log = logging.getLogger("chat2todo.ai")

#: 预设模板：只回填「地址 + 模型」，密钥永远由用户自己填。
#: 只承诺兼容标准 OpenAI /v1/chat/completions，厂商私有扩展不做适配。
PRESETS: dict[str, dict[str, str]] = {
    "豆包": {"base_url": "https://ark.cn-beijing.volces.com/api/v3",
             "model": "doubao-pro-32k"},
    "ChatGPT": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "DeepSeek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "通义千问": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model": "qwen-plus"},
    "Ollama": {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b"},
    "自定义": {"base_url": "", "model": ""},
}


def probe(base_url: str, api_key: str, model: str, timeout: int = 15,
          temperature: float = 0.0, top_p: float = 1.0,
          max_tokens: int = 32) -> tuple[bool, str]:
    """连接测试：发一条最小请求，返回 (是否可用, 给人看的说明)。

    复用与正式调用相同的出口白名单与「不跟随重定向」策略，
    避免测试通过、实际调用却被拦。
    """
    base = (base_url or "").rstrip("/")
    if not base or not url_allowed(base):
        return False, "Base URL 不合法：必须是 https，或本机 / 内网 http 地址"
    if not api_key and "127.0.0.1" not in base and "localhost" not in base:
        return False, "API Key 为空"
    payload = {
        "model": model or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "只回一个字：好"},
            {"role": "user", "content": "连接测试"},
        ],
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        latency = int((time.monotonic() - t0) * 1000)
        content = body["choices"][0]["message"]["content"]
        return True, f"连接成功 · {latency} ms · 返回「{content.strip()[:20]}」"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, f"鉴权失败（HTTP {exc.code}）：请检查 API Key"
        if exc.code == 404:
            return False, "接口不存在（HTTP 404）：请检查 Base URL 是否以 /v1 结尾"
        return False, f"接口返回 HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, f"连接失败：{exc.reason}"
    except (KeyError, IndexError, ValueError, TypeError, OSError) as exc:
        return False, f"响应解析失败：{type(exc).__name__}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """拒绝一切重定向。

    urlopen 默认跟随 302，会把 Authorization 头原样带到新地址——
    配置的端点只要回一个 302，Bearer <api_key> 就送给了攻击者。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def url_allowed(url: str) -> bool:
    """出口白名单：https 放行任意主机；明文 http 只允许本机或内网。

    内网是给 Ollama 这类本地模型留的；公网 http 等于把聊天原文裸奔上网。
    """
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    if p.scheme == "https":
        return True
    if host in ("localhost", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False        # http + 公网域名 → 拒绝
    return ip.is_loopback or ip.is_private


class AI:
    def __init__(self, cfg: dict,
                 on_call: Optional[Callable[[dict], None]] = None) -> None:
        ai = dict(AI_DEFAULTS)
        ai.update(cfg.get("ai") or {})
        # 多档案：当前生效的档案覆盖上面的默认值（密钥等字段一并覆盖）
        profile_name = str(ai.get("profile") or "default")
        profiles = ai.get("profiles") or {}
        if isinstance(profiles, dict) and isinstance(profiles.get(profile_name), dict):
            ai.update(profiles[profile_name])
        self.profile = profile_name
        self.cfg = ai
        self.enabled = _as_bool(ai.get("enabled"))
        raw_base = str(ai.get("base_url") or "").rstrip("/")
        # 不安全的主机直接置空 → usable 为 False → 一个请求都不会发
        self.base_url = raw_base if raw_base and url_allowed(raw_base) else ""
        self.base_url_rejected = bool(raw_base and not self.base_url)
        self.api_key = str(ai.get("api_key") or "")
        import os

        self.api_key = self.api_key or os.environ.get("C2T_AI_KEY", "")
        self.model = str(ai.get("model") or "gpt-4o-mini")
        self.timeout = int(ai.get("timeout") or 15)
        # 采样参数：文档要求可调；默认 temperature=0 保证判定可复现
        try:
            self.temperature = float(ai.get("temperature") or 0.0)
        except (TypeError, ValueError):
            self.temperature = 0.0
        try:
            self.top_p = float(ai.get("top_p") or 1.0)
        except (TypeError, ValueError):
            self.top_p = 1.0
        try:
            self.max_tokens = max(16, int(ai.get("max_tokens") or 600))
        except (TypeError, ValueError):
            self.max_tokens = 600
        try:
            self.token_limit = max(0, int(ai.get("tokens_per_day") or 0))
        except (TypeError, ValueError):
            self.token_limit = 0
        self.used_tokens = 0          # 本进程累计，用于每日上限降级
        self.quota_exhausted = False
        self.calls = 0
        self.on_call = on_call
        self._privacy_cfg = cfg

    def _fire(self, record: dict) -> None:
        """触发 on_call 回调；任何异常吞掉，绝不阻塞主流程。"""
        if self.on_call is None:
            return
        try:
            self.on_call(record)
        except Exception:  # noqa: BLE001 - 回调失败不能影响抽取
            pass

    # --- 能力开关 -------------------------------------------------
    def on(self, feature: str) -> bool:
        return bool(self.enabled and self.cfg.get(feature))

    @property
    def usable(self) -> bool:
        return bool(self.enabled and self.base_url and self.api_key)

    # --- 底层调用 -------------------------------------------------
    def _chat(self, system: str, user: str, max_tokens: int = 256,
              timeout: Optional[int] = None, feature: str = "",
              session: str = "") -> Optional[str]:
        if not self.usable:
            return None
        # 云端调用前强制二次脱敏：白名单群也不例外，这是文档写死的红线；
        # 只有用户显式开启「AI 明文」开关（可再按群号收窄）时才放行原文
        user = redact_for_ai(user, self._privacy_cfg, session)
        # 每日 token 上限：超了就静默降级成关键词分类（返回 None 即回落）
        if self.token_limit and self.used_tokens >= self.token_limit:
            if not self.quota_exhausted:
                self.quota_exhausted = True
                log.warning("AI token 用量已达每日上限 %d，降级为关键词规则", self.token_limit)
            return None
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": min(max_tokens, self.max_tokens),
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
        t0 = time.monotonic()
        try:
            with _NO_REDIRECT_OPENER.open(req, timeout=timeout or self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            latency_ms = int((time.monotonic() - t0) * 1000)
            self.calls += 1
            usage = body.get("usage") or {}
            in_tok = int(usage.get("prompt_tokens") or 0)
            out_tok = int(usage.get("completion_tokens") or 0)
            self.used_tokens += in_tok + out_tok
            self._fire({
                "feature": feature,
                "model": self.model,
                "in_tokens": in_tok,
                "out_tokens": out_tok,
                "latency_ms": latency_ms,
                "failed": 0,
            })
            return body["choices"][0]["message"]["content"].strip()
        except (urllib.error.URLError, KeyError, IndexError, ValueError,
                TypeError, AttributeError, OSError):
            latency_ms = int((time.monotonic() - t0) * 1000)
            self._fire({
                "feature": feature,
                "model": self.model,
                "in_tokens": 0,
                "out_tokens": 0,
                "latency_ms": latency_ms,
                "failed": 1,
            })
            return None

    # --- 五个环节 -------------------------------------------------
    def review(self, text: str, session: str = "") -> Optional[bool]:
        """是不是真的要我做？返回 None 表示无法判断，调用方保留原结论。"""
        out = self._chat(
            "你是待办判定器。只回一个字：是 或 否。"
            "判定标准：只要这句话在提醒、通知或要求读者去做某件事，就算“是”；"
            "面向全体的通知（如“大家记得报名”）也算；纯闲聊、纯陈述事实算“否”。",
            f"这句话是否包含需要读者去做的事：\n{text}",
            max_tokens=4, feature="review", session=session,
        )
        if not out:
            return None
        return out.startswith("是")

    def intent(self, text: str, session: str = "") -> Optional[bool]:
        """True 表示「有需要我去做的事」，False 表示「只是说话人自己的事」。"""
        out = self._chat(
            "判断这句话里有没有需要读者（我）去做的事。"
            "面向全体的提醒、通知、要求也算（例如“大家记得报名”）。"
            "只有纯粹是说话人陈述自己要做什么时才回 B。只回 A 或 B。"
            "A=有我该做的事，B=只是说话人自己的事。",
            text,
            max_tokens=4, feature="intent", session=session,
        )
        if not out:
            return None
        return out.strip().upper().startswith("A")

    def rewrite(self, text: str, session: str = "") -> Optional[str]:
        out = self._chat(
            "把口语化的要求改写成一条可执行待办，格式：时间 + 动作 + 对象。"
            "保留原文信息，不要编造。只输出改写结果一行。",
            text,
            max_tokens=80, feature="rewrite", session=session,
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
            max_tokens=600, feature="refine_ocr",
            timeout=max(int(self.timeout), 120),   # OCR 文本长，单次放宽超时
        )
        return out.strip() if out else None

    def merge(self, texts: list[str], session: str = "") -> Optional[list[str]]:
        """把讲同一件事的多条合并；返回合并后的列表。"""
        if len(texts) < 2:
            return None
        numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, 1))
        out = self._chat(
            "下面若干条待办里，有些其实在说同一件事。请把说同一件事的合并成一条，"
            "其余原样保留。只输出 JSON 数组，例如 [\"合并后的待办\"]。",
            numbered,
            max_tokens=300, feature="merge", session=session,
        )
        if not out:
            return None
        try:
            data = json.loads(out[out.find("["): out.rfind("]") + 1])
            return [str(x) for x in data] if isinstance(data, list) else None
        except (ValueError, IndexError):
            return None
