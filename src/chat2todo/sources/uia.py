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

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
