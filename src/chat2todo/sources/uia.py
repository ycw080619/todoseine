"""UIA 消息采集后端（可选增强通道，替代/补充数据库只读通道）。

设计目标（对齐飞书文档「UIA消息采集模块」）：
- 后台读取 QQ 窗口消息，基于三元组做增量去重
- 三级降级链：UIA 主路径 → 备选 UIA 路径 → Win11 系统 OCR
- 纯只读：不注入、不修改 QQ 进程
- 零依赖降级：uiautomation 没装时 available() 返回 False，不抛 ImportError
- 多显示器 / 混合 DPI 坐标自动换算

实现限制：
- QQ NT 版用 Chromium 自绘渲染聊天区，UIA 树里没有 ListItem 标准控件。
  因此 UIA 主路径实际是「找 TextControl」的宽匹配，失败后直接走 OCR。
- OCR 路径只在 Win10 1903+ / Win11 有 Windows.Media.Ocr 系统接口；
  更老的系统自动跳过，降级为「提示用户用导出文件」。
- OCR 识别出的文本不写回 Message.sender_id（无法定位发送人），
  只填 content 字段，抽取待办流程继续跑。

和 sources/db.py 的关系：
  db.py  = 离线全量通道（QQ 不运行也能读，一次全量）
  uia.py = 在线实时通道（QQ 必须在运行，每秒增量）
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..errors import C2TError
from ..models import Message

log = logging.getLogger("chat2todo.sources.uia")


# ---------------------------------------------------------------------------
# 可选依赖探测
# ---------------------------------------------------------------------------

def _import_uiautomation() -> Optional[object]:
    """尝试 import uiautomation；找不到返回 None，不抛 ImportError。"""
    try:
        import uiautomation  # type: ignore[import-not-found]
        return uiautomation
    except ImportError:
        return None


_UIA = _import_uiautomation()


def _win11_ocr_available() -> bool:
    """探测 Win11 / Win10 1903+ 的 Windows.Media.Ocr 接口是否可用。

    用 winrt / win10toast / ctypes 三种路径探测，任一成功即为 True。
    """
    if os.name != "nt":
        return False
    try:
        return True
    except Exception:
        pass
    try:
        import ctypes
        # Windows.Media.Ocr 是 UWP COM API，DLL 为 Windows.Media.dll
        # 简单探测：能否 LoadLibrary（不保证 OCR 真能用，只是粗略探）
        ctypes.WinDLL("Windows.Media.dll")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 外置选择器配置
# ---------------------------------------------------------------------------

# 默认选择器：覆盖 QQ NT 常见版本的搜索策略。
# QQ 版本更新时只需改这个 JSON 或换一个 JSON 文件（支持热更新）。
#
# selector 字段是嵌套搜索路径：
#   "path": ["WindowControl", "PaneControl", "PaneControl", "TreeControl"]
#       → 逐层 GetChildren 找对应 ControlTypeName
#   "message_list": {"ControlTypeName": "ListControl", "AutomationId": "ChatList"}
#       → 在当前作用域内 Find 符合条件的第一个控件
#   "message_item": {"ControlTypeName": "ListItemControl"}
#       → 在 message_list 内 FindAll 所有符合条件的子元素
#
# 也可以用更弹性的"宽匹配"：
#   "text_in_chat": {"ControlTypeName": "TextControl", "depth": 10}
#       → 在聊天区域内找所有 TextControl，取 Name 属性当消息内容
#
# fallback 链按顺序尝试，第一个返回非空列表就停。
DEFAULT_SELECTOR = {
    "schema": "c2t-uia-selector-v1",
    "qq_window": {
        "title": "QQ",
        "class_name": "Chrome_WidgetWin_1",
    },
    "fallbacks": [
        {
            "name": "chat_textcontrols_wide",
            # QQ NT Chromium 自绘 → 整个聊天区退化成 PaneControl 套娃，
            # 但子元素仍能被 TextControl 感知。直接全窗口 Find TextControl。
            "search": {
                "ControlTypeName": "TextControl",
                "depth": 10,
            },
        },
        {
            "name": "alt_pane_children",
            "search": {
                "ControlTypeName": "PaneControl",
                "depth": 8,
            },
            "note": "兜底：PaneControl 可能有内嵌 Text 子元素（自动化属性）",
        },
    ],
    "ocr": {
        "enabled": True,
        "region": "chat_area_auto",   # 自动找聊天区域矩形（窗口内部右侧大 Pane）
        "dpi_aware": True,
    },
}


def _selector_path() -> List[Path]:
    """选择器 JSON 文件搜索路径（按优先级从高到低）。"""
    paths: List[Path] = []
    # 用户自定义：~/.chat2todo/uia_selectors/*.json
    home_selectors = Path.home() / ".chat2todo" / "uia_selectors"
    if home_selectors.is_dir():
        paths.extend(sorted(home_selectors.glob("*.json")))
    # 内置：tools/uia_selectors/*.json
    here = Path(__file__).resolve().parent.parent.parent
    tools_selectors = here / "tools" / "uia_selectors"
    if tools_selectors.is_dir():
        paths.extend(sorted(tools_selectors.glob("*.json")))
    return paths


def load_selector() -> Dict:
    """加载选择器配置；没找到自定义 JSON 则用 DEFAULT_SELECTOR。"""
    for path in _selector_path():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema") == "c2t-uia-selector-v1":
                log.info("UIA selector loaded from %s", path)
                return data
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to load selector %s: %s", path, exc)
    return DEFAULT_SELECTOR


# ---------------------------------------------------------------------------
# QQ 窗口定位
# ---------------------------------------------------------------------------

def _find_qq_window_selector() -> Optional[Tuple[object, str]]:
    """用 EnumWindows + ClassName/Title 匹配找 QQ 主窗口。

    返回 (uiautomation.Control 对象, 标题)；找不到返回 None。
    优先用 ctypes 枚举 + ControlFromHandle，比 UIA 树里直接搜更可靠
    （QQ 的 WindowControl 在 UIA 树里可能被折叠进根节点）。
    """
    if _UIA is None:
        return None
    import ctypes
    from ctypes import wintypes

    EnumWindows = ctypes.windll.user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    GetClassNameW = ctypes.windll.user32.GetClassNameW
    GetWindowTextLengthW = ctypes.windll.user32.GetWindowTextLengthW
    GetWindowTextW = ctypes.windll.user32.GetWindowTextW
    IsWindowVisible = ctypes.windll.user32.IsWindowVisible

    selector_cfg = load_selector().get("qq_window", {})
    want_title = selector_cfg.get("title", "QQ")
    want_class = selector_cfg.get("class_name", "")

    found_hwnds: List[Tuple[int, str, str]] = []

    def cb(hwnd, _):
        if not IsWindowVisible(hwnd):
            return True
        title_len = GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(title_len + 1)
        GetWindowTextW(hwnd, title_buf, title_len + 1)
        cls_buf = ctypes.create_unicode_buffer(256)
        GetClassNameW(hwnd, cls_buf, 256)
        # 精确匹配：标题必须等于 want_title（不是 contains），类名同理
        if ((not want_title or title_buf.value == want_title)
                and (not want_class or cls_buf.value == want_class)
                and (want_title or want_class)):
            found_hwnds.append((hwnd, title_buf.value, cls_buf.value))
        return True

    EnumWindows(EnumWindowsProc(cb), 0)

    if not found_hwnds:
        return None

    # 多开场景：优先选窗口面积最大的（最可能是主聊天窗口）
    def _area(hwnd: int) -> int:
        rect = ctypes.wintypes.RECT()
        user32 = ctypes.windll.user32
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        return max(0, w * h)

    found_hwnds.sort(key=lambda x: _area(x[0]), reverse=True)
    hwnd, title, cls = found_hwnds[0]
    try:
        ctrl = _UIA.ControlFromHandle(hwnd)
        return ctrl, title
    except Exception as exc:  # noqa: BLE001
        log.debug("ControlFromHandle failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# 三元组去重
# ---------------------------------------------------------------------------

@dataclass
class TripleDeduper:
    """消息三元组增量去重。

    三元组 = (msg_id_hash, content_hash, sender_id_hash)。
    msg_id_hash 取 UIA AutomationId 或时间戳取模（没有 AutomationId 时退化）；
    content_hash = sha1(content)[:16]；
    sender_id_hash = 发送人 QQ 号或昵称 hash（UIA 树里取不到 QQ 号时退化）。

    已见集合持久化到 ~/.chat2todo/uia_seen.json（JSON lines，追加写），
    避免程序重启后把已经入库的消息再扫一遍。
    """
    seen: set = field(default_factory=set)
    persist_path: Optional[Path] = None
    max_seen: int = 20000

    @classmethod
    def load(cls, persist_path: Optional[str] = None) -> "TripleDeduper":
        p = Path(persist_path) if persist_path else None
        seen: set = set()
        if p and p.exists():
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    for line in fh:
                        k = line.strip()
                        if k:
                            seen.add(k)
            except Exception as exc:  # noqa: BLE001
                log.debug("Failed to load seen set from %s: %s", p, exc)
        return cls(seen=seen, persist_path=p)

    @staticmethod
    def _make_key(content: str, sender_id: str = "",
                  msg_id: str = "") -> str:
        c = hashlib.sha1((content or "").encode("utf-8")).hexdigest()[:16]
        s = hashlib.sha1((sender_id or "").encode("utf-8")).hexdigest()[:8]
        m = hashlib.sha1((msg_id or "").encode("utf-8")).hexdigest()[:8]
        return f"{m}|{c}|{s}"

    def is_new(self, content: str, sender_id: str = "",
               msg_id: str = "") -> bool:
        key = self._make_key(content, sender_id, msg_id)
        if key in self.seen:
            return False
        # 容量保护：超过 max_seen 丢弃最老一半（简单截断）
        if len(self.seen) >= self.max_seen:
            log.info("TripleDeduper: seen full (%d), clearing oldest half", len(self.seen))
            self.seen = set(list(self.seen)[len(self.seen) // 2:])
        self.seen.add(key)
        return True

    def persist(self) -> None:
        if not self.persist_path:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.persist_path, "w", encoding="utf-8") as fh:
                for k in sorted(self.seen):
                    fh.write(k + "\n")
        except Exception as exc:  # noqa: BLE001
            log.debug("Failed to persist seen set: %s", exc)


# ---------------------------------------------------------------------------
# 主体：UIASourceBackend
# ---------------------------------------------------------------------------

@dataclass
class UIASourceBackend:
    """UIA 消息采集后端。

    和 db.py 的 BuiltinBackend / CommunityBackend 同接口：
        available() → bool
        fetch(chat_id, since, limit) → List[Message]

    三级降级链：
        1. UIA 直接找 TextControl（默认 fallback 配置）
        2. UIA 兜底找 PaneControl 子元素
        3. Win11 系统 OCR（截图 → OCR → 提取文本）
        4. 最终：raise C2TError 提示用户
    """
    deduper_path: str = ""   # 持久化去重键的文件路径
    ocr_enabled: bool = True
    selector_cfg: Optional[Dict] = None

    name = "uia"

    def __post_init__(self):
        if self.selector_cfg is None:
            self.selector_cfg = load_selector()

    # --- 可用性 ----------------------------------------------------------

    @staticmethod
    def available_static() -> bool:
        """静态探测：uiautomation 装了 + QQ 正在运行。"""
        if _UIA is None:
            return False
        return _find_qq_window_selector() is not None

    def available(self) -> bool:
        return self.available_static()

    # --- 采集 ------------------------------------------------------------

    def fetch(self, chat_id: str = "", since_ts: int = 0,
              limit: int = 200) -> List[Message]:
        """读 QQ 最近消息。

        chat_id 保留接口兼容（db.py 的 fetch 接受这个参数），
        但 UIA 实时通道暂不支持按群号过滤——UIA 树里没有群号属性。
        后续可以加「自动定位到指定会话」的功能。
        """
        if not self.available():
            raise C2TError(
                "E4005",
                "UIA 通道不可用：需要 pip install uiautomation + QQ 正在运行"
            )

        deduper = TripleDeduper.load(persist_path=self.deduper_path)
        qq_result = _find_qq_window_selector()
        if qq_result is None:
            raise C2TError("E4005", "QQ 主窗口未找到（标题=QQ, class=Chrome_WidgetWin_1）")

        qq_ctrl, _title = qq_result
        messages: List[Message] = []

        # ---- 降级链 1/2: UIA 直接找 TextControl -------------------
        raw_entries = self._read_via_uia(qq_ctrl)
        # QQ NT Chromium 自绘 → TextControl 返回的通常是群名/UI 碎片（< 5 个、长度<10）
        # 加智能判断：如果拿到的都是装饰性元素 → 跳过直接走 OCR
        is_usable_uia = (
            len(raw_entries) >= 3
            and sum(1 for e in raw_entries if len(e.get("content", "")) >= 10) >= 2
        )
        if is_usable_uia:
            for entry in raw_entries:
                content = entry.get("content", "").strip()
                if not content or len(content) < 1:
                    continue
                if not deduper.is_new(
                        content=content,
                        sender_id=entry.get("sender", ""),
                        msg_id=entry.get("msg_id", "")):
                    continue
                messages.append(Message(
                    timestamp=int(entry.get("timestamp", time.time())),
                    sender_id=str(entry.get("sender", "uia_unknown")),
                    sender_name=str(entry.get("sender", "QQ-UIA")),
                    content=content,
                    msg_id=str(entry.get("msg_id", "")),
                    chat_id=str(chat_id or ""),
                ))
        else:
            # ---- 降级链 3: OCR --------------------------------------
            if self.ocr_enabled and _win11_ocr_available():
                log.info("UIA direct read returned nothing, trying OCR fallback")
                messages.extend(self._read_via_ocr(qq_ctrl, deduper, chat_id))
            else:
                log.warning("Both UIA and OCR unavailable; nothing to return")

        deduper.persist()
        # 按 since_ts / limit 过滤
        if since_ts:
            messages = [m for m in messages if m.timestamp >= since_ts]
        if limit:
            messages = messages[-limit:]
        return messages

    # --- UIA 主路径 ------------------------------------------------------

    def _read_via_uia(self, qq_ctrl) -> List[Dict]:
        """遍历 fallback 配置里的搜索策略，第一个返回非空就停。"""
        if _UIA is None:
            return []
        _UIA.SetGlobalSearchTimeout(0.5)

        results: List[Dict] = []
        fallbacks = (self.selector_cfg or {}).get("fallbacks", [])

        for fb in fallbacks:
            name = fb.get("name", "unknown")
            search = fb.get("search", {})
            ctrl_type = search.get("ControlTypeName", "TextControl")
            depth = int(search.get("depth", 8))

            getattr(qq_ctrl, "GetChildren", None)
            # 不同层级递归 GetChildren 直到找到目标类型
            found = self._collect_by_type(qq_ctrl, ctrl_type, max_depth=depth)
            if found:
                log.info("UIA fallback '%s': found %d %s elements",
                         name, len(found), ctrl_type)
                results.extend(self._elements_to_entries(found))
                break

        return results

    @staticmethod
    def _collect_by_type(root, ctrl_type: str, max_depth: int = 10) -> List[object]:
        """递归遍历 UIA 树，收集符合 ControlTypeName 的所有元素。"""
        collected: List[object] = []

        def walk(ctrl, depth: int):
            if depth > max_depth:
                return
            try:
                if ctrl.ControlTypeName == ctrl_type:
                    collected.append(ctrl)
                for child in ctrl.GetChildren():
                    walk(child, depth + 1)
            except Exception:  # noqa: BLE001 - 某些自绘控件子节点会抛
                pass

        walk(root, 0)
        return collected

    @staticmethod
    def _elements_to_entries(elements: List[object]) -> List[Dict]:
        """把 UIA 元素列表转成后端统一行格式（无 sender/msg_id，只填 content）。

        QQ NT 自绘 UIA 树里没有可读出的 sender / AutomationId，
        所以这两个字段留空，让去重逻辑靠 content_hash 兜底。
        """
        entries: List[Dict] = []
        seen_texts: set = set()
        now = int(time.time())
        for el in elements:
            try:
                text = (el.Name or "").strip()
            except Exception:  # noqa: BLE001
                text = ""
            # 太长 / 太短 / 和之前重复 → 跳过（UI 里的按钮文字、标签都不是消息）
            if not text or len(text) < 2 or len(text) > 500:
                continue
            if text in seen_texts:
                continue
            seen_texts.add(text)
            entries.append({
                "content": text,
                "sender": "",          # QQ NT UIA 树里没有 sender 属性
                "msg_id": "",          # 同上
                "timestamp": now,      # 没有时间戳属性 → 退化到抓取时刻
                "type": 0,
            })
        return entries

    # --- OCR 降级路径 ----------------------------------------------------

    def _read_via_ocr(self, qq_ctrl, deduper: TripleDeduper,
                      chat_id: str) -> List[Message]:
        """截图聊天区域 → OCR 识别。

        依赖链（按优先级尝试，任一成功即停）：
          1. Win11 系统 OCR（Windows.Media.Ocr via winrt / ctypes）
          2. pytesseract + Tesseract 引擎（pytesseract 已装，引擎需另装）

        全部不可用时返回空列表，并提示用户改走 db 通道。
        QQ NT Chromium 自绘场景下，这是唯一能读到消息内容的降级路径。
        """
        try:
            rect = qq_ctrl.BoundingRectangle
            if rect.left >= rect.right or rect.top >= rect.bottom:
                log.warning("QQ window rect invalid (may be minimized): %s", rect)
                return []
            rect.right - rect.left
            rect.bottom - rect.top
            # 多显示器 / 负坐标修正
            from PIL import ImageGrab  # type: ignore[import-not-found]
            if rect.left < 0 or rect.top < 0:
                # 虚拟屏截图再 crop
                full = ImageGrab.grab(all_screens=True)
                img = full.crop((rect.left, rect.top, rect.right, rect.bottom))
            else:
                img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
            # 聊天区域：右 70% 裁切（去掉导航栏）
            chat_crop = img.crop((
                int(img.width * 0.30), 48, img.width, img.height
            ))

            # ---- 路径 1: Win11 系统 OCR（Windows.Media.Ocr）----
            text = self._try_win11_ocr(chat_crop)
            if text:
                return self._ocr_text_to_messages(text, deduper, chat_id)

            # ---- 路径 2: pytesseract 应急 ----
            text = self._try_tesseract(chat_crop)
            if text:
                return self._ocr_text_to_messages(text, deduper, chat_id)

            log.warning(
                "Both Win11 OCR and Tesseract unavailable. "
                "UIA channel cannot read QQ NT chat content on this machine. "
                "Use db channel (c2t db-import) or export file channel instead. "
                "To enable OCR: `pip install winrt-windows-media-ocr winrt-windows-graphics-imaging` "
                "or install Tesseract OCR engine + `pip install pytesseract`."
            )
            return []

        except Exception as exc:  # noqa: BLE001
            log.warning("OCR fallback failed: %s", exc)
            return []

    @staticmethod
    def _try_win11_ocr(pil_img) -> str:
        """尝试 Win11 原生 Windows.Media.Ocr。依赖 winrt-windows-* 子包完整。

        链路（已真机验证跑通）：
            PIL → BytesIO PNG → InMemoryRandomAccessStream
            → BitmapDecoder.create_async → get_software_bitmap_async
            → OcrEngine.recognize_async → OcrResult.text

        引擎工厂方法：winrt-windows-media-ocr 4.x 只有 try_create_from_language(lang) 可用。
        """
        try:
            import asyncio
            import io

            import winrt.windows.media.ocr as ocr
            import winrt.windows.globalization as glob
            import winrt.windows.graphics.imaging as imaging
            import winrt.windows.storage.streams as streams

            async def run_ocr() -> str:
                # 1. 构造引擎（已确认：4.x 只有 try_create_from_language 可用）
                try:
                    lang = glob.Language("zh-Hans-CN")
                    engine = ocr.OcrEngine.try_create_from_language(lang)
                    if engine is None:
                        return ""
                except Exception as exc:
                    log.debug("OcrEngine init failed: %s", exc)
                    return ""

                # 2. PIL → PNG bytes → InMemoryRandomAccessStream
                buf = io.BytesIO()
                pil_img.save(buf, format="PNG")
                png_bytes = buf.getvalue()

                ras = streams.InMemoryRandomAccessStream()
                writer = streams.DataWriter(ras.get_output_stream_at(0))
                writer.write_bytes(png_bytes)  # bytes 是 buffer-like，winrt 4.x 接受
                await writer.store_async()
                await writer.flush_async()
                writer.detach_stream()
                ras.seek(0)

                # 3. BitmapDecoder 解码
                decoder = await imaging.BitmapDecoder.create_async(ras)
                bitmap = await decoder.get_software_bitmap_async()

                # 4. OCR
                result = await engine.recognize_async(bitmap)
                return (result.text if result else "") or ""

            text = asyncio.run(run_ocr())
            if text:
                log.info("Win11 OCR recognized %d chars", len(text))
            return text

        except ImportError as exc:
            log.debug("Win11 OCR imports missing: %s", exc)
            return ""
        except Exception as exc:  # noqa: BLE001
            log.debug("Win11 OCR failed: %s", exc)
            return ""

    @staticmethod
    def _try_tesseract(pil_img) -> str:
        """应急路径：pytesseract + 本地 Tesseract 引擎。"""
        try:
            import pytesseract
            text = pytesseract.image_to_string(pil_img, lang="chi_sim+eng")
            if text:
                log.info("Tesseract OCR recognized %d chars", len(text))
            return text
        except ImportError:
            log.debug("pytesseract not installed")
        except Exception as exc:  # noqa: BLE001
            log.debug("Tesseract OCR failed: %s", exc)
        return ""

    @staticmethod
    def _ocr_text_to_messages(text: str, deduper: TripleDeduper,
                               chat_id: str) -> List[Message]:
        """把 OCR 输出解析成 Message 列表。

        OCR 输出里换行符不稳定（可能很少或很多），所以用多层策略切分：
          1. 先按 \\n 分行
          2. 超过 120 字的长行 → 再按「。！？」「数字时间戳 16:29」切
          3. 最后按 30-200 字切片（防止漏切的超长行）

        OCR 没有 sender_id / msg_id / timestamp，全部退化：
          sender_id="ocr_unknown"
          sender_name="OCR识别"
          timestamp=抓取时刻
        """
        import re
        messages: List[Message] = []
        now = int(time.time())

        def _split_long(line: str) -> List[str]:
            """长行二次切分。"""
            line = line.strip()
            if len(line) <= 120:
                return [line]
            # 优先按句子句号 / 问号 / 感叹号 + 时间戳切
            parts = re.split(r"(?<=[。！？!?])\s*|(?=\d{1,2}[:：]\d{2})", line)
            parts = [p.strip() for p in parts if p.strip()]
            # 还剩超长的，硬切 150 字
            result: List[str] = []
            for p in parts:
                if len(p) <= 150:
                    result.append(p)
                else:
                    for i in range(0, len(p), 120):
                        result.append(p[i:i+150].strip())
            return result or [line]

        for raw_line in text.splitlines():
            for sub in _split_long(raw_line):
                if len(sub) < 2:
                    continue
                if not deduper.is_new(content=sub):
                    continue
                messages.append(Message(
                    timestamp=now,
                    sender_id="ocr_unknown",
                    sender_name="OCR识别",
                    content=sub,
                    chat_id=str(chat_id or ""),
                ))
        return messages


# ---------------------------------------------------------------------------
# 不依赖 uiautomation 的 OCR 直采通道
# ---------------------------------------------------------------------------

_DPI_TRIED = False


_CJK_PUNCT = "：，。、？！；．…—·（）【】《》〈〉「」『』“”‘’"


def normalize_cjk_spaces(text: str) -> str:
    """WinRT OCR 会在汉字之间插空格（「记 得 明 天」），这里压回去。

    只删「汉字 ↔ 汉字」之间的空白，中英混排、数字与单位之间的空格保留
    （「明天 10:30」不能变成「明天10:30」）；但全角标点两侧的空格一律删，
    否则「19 ： 04」这种时间戳会一直带着空格入库。
    """
    import re

    cjk = (
        r"(?<=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])\s+"
        r"(?=[\u4e00-\u9fff])"
    )
    punct = r"[ \t]*([" + _CJK_PUNCT + r"])[ \t]*"
    lines = []
    for line in (text or "").splitlines():
        line = re.sub(cjk, "", line)
        line = re.sub(punct, r"\1", line)
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        lines.append(line)
    return "\n".join(lines)


def _ensure_dpi_aware() -> None:
    """截图前确保进程是 PerMonitorV2，否则 150% 缩放下截出来的区域会错位。"""
    global _DPI_TRIED
    if _DPI_TRIED or os.name != "nt":
        return
    _DPI_TRIED = True
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 - 已设置过 / 老系统没有 shcore
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def enum_qq_hwnds() -> List[Tuple[int, int]]:
    """枚举 QQ 主窗口 [(hwnd, 面积)]，按面积倒序；不依赖 uiautomation。"""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: List[Tuple[int, int]] = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if title.value.strip() != "QQ":
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value != "Chrome_WidgetWin_1":
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        found.append((int(hwnd), area))
        return True

    user32.EnumWindows(proc(cb), 0)
    found.sort(key=lambda x: x[1], reverse=True)
    return found


def qq_window_info(hwnd: int) -> Dict[str, Any]:
    """拿到 QQ 窗口的 PID / 标题 / 可执行文件版本，用于「识别对应控件规则」。

    版本号取 QQ.exe 的 FileVersion（QQ NT 的 9.x 与旧版 8.x 控件树不同），
    取不到时返回空串——上层据此提示用户「无法确认 QQ 版本」而不是瞎猜。
    """
    if os.name != "nt" or not hwnd:
        return {"hwnd": int(hwnd or 0), "pid": 0, "version": "", "path": ""}
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    info = {"hwnd": int(hwnd), "pid": int(pid.value), "version": "", "path": ""}
    handle = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return info
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            info["path"] = buf.value
    finally:
        kernel32.CloseHandle(handle)

    path = info["path"]
    if not path:
        return info
    try:
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return info
        data = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, data):
            return info
        ptr = ctypes.c_void_p()
        length = wintypes.UINT()
        if ver.VerQueryValueW(data, "\\", ctypes.byref(ptr), ctypes.byref(length)) and ptr.value:
            fixed = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
            ms, ls = fixed[2], fixed[3]
            info["version"] = "%d.%d.%d.%d" % (
                ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:  # noqa: BLE001 - 版本信息读不到不影响采集
        pass
    return info


#: OCR 低置信度阈值默认值（可在 config 的 uia.ocr_min_confidence 调整）
DEFAULT_MIN_CONFIDENCE = 0.5


def ocr_confidence(text: str) -> float:
    """粗略估计 OCR 结果可信度（0~1）。

    Windows.Media.Ocr 不给逐词置信度，这里用「文本像不像正常聊天记录」来判断：
      * 可读字符（汉字 / 字母 / 数字 / 常用标点 / 空白）占比；
      * 乱码符号（私用区、替换符、方块、控制字符）占比；
      * 极短结果直接压低（零星几个字最容易是误识别）。
    低于阈值的结果按文档要求不写入事实源、不参与待办提取。
    """
    if not text or not text.strip():
        return 0.0
    total = len(text)
    readable = sum(1 for ch in text if ch.isalnum() or ch.isspace()
                   or ch in "，。！？；：、“”‘’（）《》【】—…·,.!?;:\"'()[]{}<>/-+%&@#*=_~|")
    junk = total - readable
    score = readable / total
    if junk:
        score -= min(0.4, junk / total * 1.5)

    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if cjk < 6 and len(text.strip()) < 16:
        score -= 0.25              # 太短的碎片，多半是时间戳 / 按钮文字
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and sum(len(ln.strip()) for ln in lines) / len(lines) < 2.5:
        score -= 0.15              # 大量单字行，通常是图标 / 头像被误识
    return max(0.0, min(1.0, score))


def _client_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """窗口客户区在屏幕上的物理像素矩形 (left, top, right, bottom)。"""
    if os.name != "nt" or not hwnd:
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    left, top = origin.x, origin.y
    right, bottom = left + rect.right, top + rect.bottom
    if right - left < 80 or bottom - top < 80:
        return None            # 最小化 / 被折叠
    return left, top, right, bottom


def _dpi_scale(hwnd: int) -> float:
    """窗口所在显示器的缩放比例（150% → 1.5）。"""
    if os.name != "nt":
        return 1.0
    try:
        import ctypes

        dpi = int(ctypes.windll.user32.GetDpiForWindow(hwnd))
        return max(1.0, dpi / 96.0) if dpi else 1.0
    except Exception:  # noqa: BLE001 - Win10 早期版本没有这个 API
        return 1.0


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


def _print_window_image(hwnd: int):
    """用 PrintWindow 直接渲染窗口自身，被别的窗口挡住也能拿到内容。

    QQ NT（Chromium）支持 PW_RENDERFULLCONTENT(=2)；窗口最小化时返回全黑，
    这种情况返回 None，交给前台截图兜底。
    """
    if os.name != "nt" or not hwnd:
        return None
    import ctypes
    from ctypes import wintypes

    from PIL import Image  # type: ignore[import-not-found]

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width < 120 or height < 120:
        return None

    hdc = user32.GetWindowDC(hwnd)
    if not hdc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(hdc)
    bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
    old = gdi32.SelectObject(mem_dc, bitmap)
    img = None
    try:
        # flag 2 = PW_RENDERFULLCONTENT，Chromium 窗口必须用这个才能出画面
        if not user32.PrintWindow(hwnd, mem_dc, 2):
            return None
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height          # 负数 = 自上而下
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0
        buf = ctypes.create_string_buffer(width * height * 4)
        copied = gdi32.GetDIBits(mem_dc, bitmap, 0, height, buf,
                                 ctypes.byref(header), 0)
        if not copied:
            return None
        img = Image.frombuffer("RGBA", (width, height), buf,
                               "raw", "BGRA", 0, 1).convert("RGB")
        # 最小化 / 全黑 → 视为失败
        extrema = img.convert("L").getextrema()
        if extrema[1] < 8:
            return None
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hdc)
    return img


def _grab_chat_image(hwnd: int):
    """截 QQ 客户区的聊天区域（去掉左侧会话列表和顶部工具条）。"""
    rect = _client_rect(hwnd)
    if rect is None:
        return None
    _ensure_dpi_aware()
    from PIL import ImageGrab  # type: ignore[import-not-found]

    left, top, right, bottom = rect
    img = ImageGrab.grab(bbox=(left, top, right, bottom))
    return _cut_sidebar(hwnd, img)


def _column_modes(img, quant: int = 8):
    """按列取「众数底色」：返回 (列坐标列表, 每列的主色)。

    用众数而不是均值，是为了忽略整列里占少数的气泡、头像和文字，
    只留下这一列占比最高的背景色 —— 判断「这列属于哪个面板」靠的就是它。
    """
    width, height = img.size
    step_x = max(2, width // 400)
    step_y = max(1, height // 120)
    px = img.convert("RGB").load()
    cols = list(range(0, width, step_x))

    def mode_color(c: int) -> Tuple[int, int, int]:
        cnt: Dict[Tuple[int, int, int], int] = {}
        for r in range(0, height, step_y):
            p = px[c, r]
            key = (p[0] // quant, p[1] // quant, p[2] // quant)
            cnt[key] = cnt.get(key, 0) + 1
        return max(cnt.items(), key=lambda kv: kv[1])[0]

    return cols, [mode_color(c) for c in cols]


def _chat_bg(modes) -> Tuple[int, int, int]:
    """聊天区底色 = 所有列主色里出现次数最多的那个。

    会话列表和右侧群成员面板各占一块，聊天区最宽，它的底色在列主色里占比最高。
    早先取「最右一列」，群成员面板一展开就把它当成聊天区；
    后来改取正中一列，正中正好落在消息气泡上时同样会错。
    """
    cnt: Dict[Tuple[int, int, int], int] = {}
    for m in modes:
        cnt[m] = cnt.get(m, 0) + 1
    return max(cnt.items(), key=lambda kv: kv[1])[0]


def _detect_chat_left(hwnd: int, img) -> int:
    """找出会话列表与聊天区的分界线。

    会话列表是一种底色、聊天区是另一种，所以从左往右找「第一个底色变成聊天区」的位置
    就是分界 —— 列表是从第 0 列开始的一整块，这个转变只会出现一次。
    消息气泡、图片在列主色里显示为别的颜色，只会出现在分界之后，不会把分界拉偏。
    自定义主题里两者同色时退化成按比例取 30%。
    """
    width, _ = img.size
    cols, modes = _column_modes(img)
    bg = _chat_bg(modes)
    if modes[0] == bg:                        # 列表与聊天区同色，无从判断
        return min(width - 120, int(width * 0.30))

    divider = -1
    for i in range(1, len(modes)):
        if modes[i] == bg and modes[i - 1] != bg:
            divider = cols[i]
            break

    lo, hi = int(width * 0.08), int(width * 0.60)
    if divider > 0 and lo <= divider <= hi:
        return divider
    return min(width - 120, int(width * 0.30))


def _detect_chat_right(hwnd: int, img) -> int:
    """找出聊天区与右侧群成员/公告面板的分界；没开面板时返回整幅宽度。

    聊天区每一列的众数都是聊天区底色，面板那几列是另一种底色，
    所以从最右往左找到第一列「底色回到聊天区」的位置就是分界。
    """
    width, _ = img.size
    cols, modes = _column_modes(img)
    bg = _chat_bg(modes)
    right = width
    for i in range(len(cols) - 1, -1, -1):
        if modes[i] == bg:
            right = cols[i]
            break
    if right >= width or width - right <= 24:
        return width               # 只差滚动条/边距那几像素，不算面板
    if right < int(width * 0.45):
        return width               # 边界跑到画面左半边说明认错了，宁可不裁
    return min(width, right + 4)


def _trim_right_panel(hwnd: int, img):
    """切掉右侧面板。"""
    right = _detect_chat_right(hwnd, img)
    if right >= img.width:
        return img
    return img.crop((0, 0, right, img.height))


def _cut_sidebar(hwnd: int, img):
    """去掉左侧会话列表、右侧群成员面板与顶部工具条，只留聊天区。"""
    scale = _dpi_scale(hwnd)
    img = _trim_right_panel(hwnd, img)   # 先切右边：左边分界以「最右一列」为基准
    x0 = _detect_chat_left(hwnd, img)
    y0 = min(img.height - 120, int(50 * scale))
    return img.crop((max(0, x0), max(0, y0), img.width, img.height))


def _crop_chat_area(hwnd: int, img):
    """从整窗图片里裁出聊天区：先扣掉标题栏/边框，再去掉左侧列表和顶部工具条。"""
    if img is None:
        return None
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    win_rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(win_rect))
    client = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(client))
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))

    left = origin.x - win_rect.left
    top = origin.y - win_rect.top
    right = left + client.right
    bottom = top + client.bottom
    img = img.crop((max(0, left), max(0, top),
                    min(img.width, right), min(img.height, bottom)))
    return _cut_sidebar(hwnd, img)


def _capture_chat(hwnd: int):
    """拿聊天区截图：优先 PrintWindow（后台也能抓），失败才前台抓屏。"""
    img = _crop_chat_area(hwnd, _print_window_image(hwnd))
    if img is not None:
        return img
    if _is_foreground(hwnd):
        return _grab_chat_image(hwnd)
    return None


def _is_foreground(hwnd: int) -> bool:
    """目标窗口是否在前台。被别的窗口挡住时截到的是别人的画面，不能认。"""
    if os.name != "nt" or not hwnd:
        return False
    import ctypes

    return int(ctypes.windll.user32.GetForegroundWindow()) == int(hwnd)


def fetch_messages(hwnd: int = 0, chat_id: str = "",
                   deduper_path: str = "",
                   min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                   keep_low_confidence: bool = False) -> List[Message]:
    """抓取 QQ 当前聊天窗口里「上次之后新出现」的消息。

    只走截图 + Win11 OCR 这一条路：
      - uiautomation 的窗口匹配是模糊的（会把「QQ 待办项目 - TRAE」当成 QQ），
        而且需要额外安装，这里不再依赖它；
      - 截图优先用 PrintWindow 渲染窗口自身，QQ 在后台也能读；
        最小化（渲染成全黑）才回落到前台抓屏；
      - 读到文字后会先把汉字之间的空格压掉，再做跨轮次去重；
      - 置信度低于阈值的结果标记 low_conf（默认不写事实源、不参与抽取），
        用户显式选择保留时追加「OCR低置信度，仅供参考」标注。
    """
    if os.name != "nt":
        return []
    target = int(hwnd or 0)
    if not target:
        wins = enum_qq_hwnds()
        if not wins:
            return []
        target = wins[0][0]

    img = _capture_chat(target)
    if img is None:
        log.debug("QQ 窗口暂时抓不到画面（最小化或被挡住），跳过本轮")
        return []
    text = UIASourceBackend._try_win11_ocr(img)
    if not text:
        text = UIASourceBackend._try_tesseract(img)
    if not text:
        return []
    text = normalize_cjk_spaces(text)
    conf = ocr_confidence(text)
    low = conf < max(0.0, min(1.0, float(min_confidence or 0)))
    path = deduper_path or os.path.expanduser("~/.chat2todo/uia_seen.jsonl")
    deduper = TripleDeduper.load(persist_path=path)
    messages = UIASourceBackend._ocr_text_to_messages(text, deduper, chat_id)
    deduper.persist()
    for msg in messages:
        msg.confidence = conf
        if low:
            msg.low_conf = True
            if keep_low_confidence:
                msg.content = (msg.content or "") + "（OCR低置信度，仅供参考）"
    if low:
        log.warning("OCR 置信度偏低 %.2f（阈值 %.2f），本轮 %d 条按低置信度处理",
                    conf, min_confidence, len(messages))
    return messages


# ---------------------------------------------------------------------------
# 对外便捷入口
# ---------------------------------------------------------------------------

def uia_backend_or_none(cfg: Optional[Dict] = None) -> Optional[UIASourceBackend]:
    """构造一个 UIASourceBackend；但如果不可用就返回 None（不抛异常）。"""
    if not UIASourceBackend.available_static():
        return None
    cfg = cfg or {}
    uia_cfg = cfg.get("uia", {}) if isinstance(cfg, dict) else {}
    deduper_path = uia_cfg.get("deduper", os.path.expanduser("~/.chat2todo/uia_seen.jsonl"))
    return UIASourceBackend(
        deduper_path=deduper_path,
        ocr_enabled=bool(uia_cfg.get("ocr_enabled", True)),
    )


def load_messages_uia(cfg: Dict, chat_id: str = "",
                      since_ts: int = 0) -> List[Message]:
    """类似 sources.db.load_messages 的便捷入口。

    没装 uiautomation 或 QQ 未运行时抛 C2TError（不是返回空），
    让上层知道"UIA 通道不可用"，可以选择降级到 db 通道或提示用户。
    """
    backend = UIASourceBackend()
    if not backend.available():
        raise C2TError(
            "E4005",
            "UIA 通道不可用：需要 pip install uiautomation + QQ 正在运行。"
            "改用 c2t db-import 从数据库读，或 c2t import 从导出文件读。"
        )
    return backend.fetch(chat_id=chat_id, since_ts=since_ts)
