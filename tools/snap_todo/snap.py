"""截图待办（可选插件）：剪贴板图片 → 识别文字 → 人工确认 → 走核心管线入库。

用法：
  1. Win+Shift+S 框选截图（图片自动进剪贴板）
  2. 双击 snap.bat，或从控制台点「截图待办」
  3. 在弹窗里确认识别文字（可改）→「提取入库」

识别引擎（自动选择）：
  - 默认 Windows 自带 OCR（离线、中文好、零安装；winsdk 在则直调，否则走 PowerShell WinRT 桥）
  - 配置 [snap] vision_model 且 Ollama 已有该视觉模型时，自动升级为「看图直提」
    （例：ollama pull qwen2.5vl:7b，然后在 c2t.toml 加 [snap] vision_model = "qwen2.5vl:7b"）

仅依赖标准库；通过公开 API（extract_todos / vector_pass / AI / Store）复用核心能力。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from ctypes import wintypes
from pathlib import Path
from typing import Optional
from urllib import request as urlreq
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"                      # 仓库源码（src 布局，免 pip install）
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
ROOT = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Chat2Todo"
CONFIG = ROOT / "c2t.toml"

VISION_PROMPT = (
    "请逐行提取图片中的文字，只保留与待办事项、任务安排、提醒、截止时间相关的句子。"
    "每行输出一条，原样摘录，不要解释、不要编号、不要翻译。没有相关内容就什么也不输出。"
)

# ---------------------------------------------------------------- 剪贴板取图

_CF_DIB = 8
_CF_DIBV5 = 17


def _dbg(msg: str) -> None:
    """调试计时：设了环境变量 C2T_SNAP_DEBUG=<文件路径> 时写入耗时。"""
    path = os.environ.get("C2T_SNAP_DEBUG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _dpi_aware() -> None:
    """让进程感知 DPI，否则 Tk 坐标是逻辑像素、GDI 抓屏是物理像素，会截错区域。"""
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_AWARE
        except Exception:  # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def _win32():
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.OpenClipboard.argtypes = [wintypes.HWND]
    u.OpenClipboard.restype = wintypes.BOOL
    u.CloseClipboard.restype = wintypes.BOOL
    u.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    u.IsClipboardFormatAvailable.restype = wintypes.BOOL
    u.GetClipboardData.argtypes = [wintypes.UINT]
    u.GetClipboardData.restype = wintypes.HANDLE
    u.GetClipboardSequenceNumber.restype = wintypes.DWORD
    k.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k.GlobalSize.argtypes = [wintypes.HGLOBAL]
    k.GlobalSize.restype = ctypes.c_size_t
    return u, k


def clipboard_dib() -> Optional[bytes]:
    """取剪贴板里的位图（DIB 字节流，含 BITMAPINFOHEADER）；没有图片返回 None。"""
    u, k = _win32()
    if not u.OpenClipboard(None):
        return None
    try:
        fmt = _CF_DIBV5 if u.IsClipboardFormatAvailable(_CF_DIBV5) else (
            _CF_DIB if u.IsClipboardFormatAvailable(_CF_DIB) else 0)
        if not fmt:
            return None
        handle = u.GetClipboardData(fmt)
        if not handle:
            return None
        ptr = k.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.string_at(ptr, k.GlobalSize(handle))
        finally:
            k.GlobalUnlock(handle)
    finally:
        u.CloseClipboard()


def _parse_dib(dib: bytes) -> dict:
    if len(dib) < 40:
        raise ValueError("剪贴板数据太短，不是标准位图")
    header, = struct.unpack_from("<I", dib, 0)
    if header < 40:
        raise ValueError("不支持的老式位图头（BITMAPCOREHEADER）")
    width, height = struct.unpack_from("<ii", dib, 4)
    bpp, = struct.unpack_from("<H", dib, 14)
    comp, = struct.unpack_from("<I", dib, 16)
    size_img, = struct.unpack_from("<I", dib, 20)
    used, = struct.unpack_from("<I", dib, 32)
    if bpp not in (24, 32) or comp not in (0, 3):
        raise ValueError(f"暂不支持的位图格式：{bpp}bpp compression={comp}")
    palette = (used or (1 << bpp)) * 4 if bpp <= 8 else 0
    masks = (12 if comp == 3 else 0) if header == 40 else 0
    offset = header + palette + masks
    stride = ((width * bpp + 31) // 32) * 4
    h = abs(height)
    if not size_img:
        size_img = stride * h
    return {"w": width, "h": h, "top_down": height < 0, "bpp": bpp,
            "offset": offset, "stride": stride, "size_img": size_img,
            "header_end": offset}


def dib_to_bmp(dib: bytes) -> bytes:
    """DIB → BMP 文件字节（加 14 字节文件头），供 WinRT BitmapDecoder 读取。"""
    info = _parse_dib(dib)
    offset = 14 + info["header_end"]
    size = offset + info["size_img"]
    return b"BM" + struct.pack("<IHHI", size, 0, 0, offset) + dib


def dib_to_png(dib: bytes) -> bytes:
    """DIB → PNG（手写编码器：zlib + struct），供 Ollama 视觉模型使用。"""
    info = _parse_dib(dib)
    w, h, bpp = info["w"], info["h"], info["bpp"]
    px = dib[info["offset"]:info["offset"] + info["size_img"]]
    stride, step = info["stride"], bpp // 8
    color_type = 6 if bpp == 32 else 2

    rows = []
    for y in range(h):
        src = (y if info["top_down"] else h - 1 - y) * stride
        line = bytearray()
        for x in range(w):
            b, g, r = px[src + x * step], px[src + x * step + 1], px[src + x * step + 2]
            line += bytes((r, g, b, px[src + x * step + 3])) if bpp == 32 else bytes((r, g, b))
        rows.append(b"\x00" + bytes(line))          # filter type 0
    raw = zlib.compress(b"".join(rows), 6)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")

# ---------------------------------------------------------------- Windows OCR

_PS_OCR = r"""
param([string]$Path)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = $null
foreach ($m in [System.WindowsRuntimeSystemExtensions].GetMethods()) {
  if ($m.Name -eq 'AsTask' -and $m.GetParameters().Count -eq 1 -and
      $m.GetParameters()[0].ParameterType.Name -like 'IAsyncOperation*') { $asTaskGeneric = $m; break }
}
function Await($op, $type) {
  $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
  $task.Wait() | Out-Null
  $task.Result
}
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
[void][Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
[void][Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
[void][Windows.Globalization.Language, Windows.Foundation, ContentType=WindowsRuntime]
$file    = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream  = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap  = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine  = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language('zh-Hans-CN')))
}
if ($null -eq $engine) { throw 'OcrEngine unavailable (no OCR language pack)' }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Write-Output $result.Text
"""


def _ocr_winsdk(bmp_path: str) -> Optional[str]:
    """用户装了 winsdk 时直调 WinRT；没装返回 None。"""
    try:
        import asyncio

        from winsdk.windows.graphics.imaging import BitmapDecoder
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.storage import FileAccessMode, StorageFile
    except ImportError:
        return None

    async def run() -> str:
        f = await StorageFile.get_file_from_path_async(bmp_path)
        s = await f.open_async(FileAccessMode.READ)
        d = await BitmapDecoder.create_async(s)
        b = await d.get_software_bitmap_async()
        r = await OcrEngine.try_create_from_user_profile_languages().recognize_async(b)
        return str(r.text)

    return asyncio.run(run())


def _ocr_powershell(bmp_path: str) -> str:
    """零安装的 PowerShell WinRT 桥（PS 5.1 可用）。"""
    fd, tmp = tempfile.mkstemp(suffix=".ps1")
    os.close(fd)
    ps1 = Path(tmp)
    ps1.write_text(_PS_OCR, encoding="ascii")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), bmp_path],
            capture_output=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        ps1.unlink(missing_ok=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip() or "PowerShell OCR 失败"
        raise RuntimeError(err.splitlines()[-1][:300])
    return proc.stdout.decode("utf-8-sig", "replace").strip()


def ocr_windows(bmp_path: str) -> str:
    text = _ocr_winsdk(bmp_path)
    return text if text is not None else _ocr_powershell(bmp_path)

# ---------------------------------------------------------------- Ollama 视觉


def _ollama_host(cfg: dict) -> str:
    """只允许本机 Ollama；配置指向云端时截图也不外发（回落到本机默认端口）。

    截图里通常带着群名、头像、手机号，属于最高敏感度数据：
    即使 ai.base_url 被设成了云端服务，OCR 也只在 127.0.0.1 上跑。
    """
    base = str((cfg.get("ai") or {}).get("base_url") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    if not base:
        return "http://127.0.0.1:11434"
    host = (urlparse(base).hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1"):
        return base
    return "http://127.0.0.1:11434"


def vision_model_available(cfg: dict) -> str:
    """配置了 [snap] vision_model 且 Ollama 里已有该模型时，返回模型名；否则空串。"""
    model = str((cfg.get("snap") or {}).get("vision_model") or "").strip()
    if not model:
        return ""
    try:
        with urlreq.urlopen(_ollama_host(cfg) + "/api/tags", timeout=3) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - Ollama 没开就当不可用
        return ""
    names = [m.get("name", "") for m in tags.get("models", [])]
    return model if any(n == model or n.split(":")[0] == model.split(":")[0] for n in names) else ""


def ocr_vision(png: bytes, cfg: dict, model: str) -> str:
    payload = json.dumps({
        "model": model, "prompt": VISION_PROMPT, "stream": False,
        "images": [base64.b64encode(png).decode("ascii")],
    }).encode("utf-8")
    req = urlreq.Request(_ollama_host(cfg) + "/api/generate", data=payload,
                         headers={"Content-Type": "application/json"})
    with urlreq.urlopen(req, timeout=180) as resp:
        return str(json.loads(resp.read().decode("utf-8")).get("response", "")).strip()

# ---------------------------------------------------------------- 确认弹窗


def _read_theme() -> str:
    """读 c2t.toml 的 [ui] theme，默认 light（与控制台共用同一配置）。"""
    if not CONFIG.exists():
        return "light"
    section = ""
    for line in CONFIG.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip()
            continue
        if section == "ui" and "=" in s:
            k, v = s.split("=", 1)
            if k.strip() == "theme":
                return "dark" if v.strip().strip("'\"").lower() == "dark" else "light"
    return "light"


def _dpi_scale() -> float:
    """当前屏幕缩放比例（96 DPI = 1.0），用于按物理像素放大窗口。"""
    try:
        import ctypes

        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        dc = u.GetDC(0)
        dpi = g.GetDeviceCaps(dc, 88)          # LOGPIXELSX
        u.ReleaseDC(0, dc)
        return max(1.0, float(dpi) / 96.0)
    except Exception:  # noqa: BLE001
        return 1.0


def confirm_dialog(text: str, engine: str = "", refine=None) -> Optional[str]:
    """预填识别文字，返回用户确认后的文本；取消返回 None。

    refine：可选的「后台 AI 整理」函数，接收当前文本返回整理后文本。
    传入时窗口**立刻弹出**，整理在后台线程跑，完成后再把内容替换进输入框，
    这样用户不用干等模型。
    """
    import tkinter as tk

    dark = _read_theme() == "dark"
    bg = "#171b22" if dark else "#fbfbfb"
    fg = "#e3e8f0" if dark else "#1f2937"
    field_bg = "#262d37" if dark else "#ffffff"
    btn_bg = "#2b333d" if dark else "#eef2f7"
    btn_fg = "#cfd8e6" if dark else "#1f3a5f"
    accent = "#3b82f6" if dark else "#1f6feb"

    result = {"text": None}
    root = tk.Tk()
    root.title("截图待办 · 确认识别结果")
    k = _dpi_scale()
    wpx, hpx = int(760 * k), int(560 * k)
    root.geometry(f"{wpx}x{hpx}+{max(0, (root.winfo_screenwidth() - wpx) // 2)}"
                  f"+{max(0, (root.winfo_screenheight() - hpx) // 2)}")
    root.configure(bg=bg)
    head = tk.Label(root,
                    text=f"识别结果（引擎：{engine or '未知'}，可直接编辑），确认后提取待办：",
                    bg=bg, fg=fg, anchor="w", font=("Microsoft YaHei UI", -int(15 * k)))
    head.pack(fill="x", padx=12, pady=(10, 4))
    box = tk.Text(root, font=("Microsoft YaHei UI", -int(16 * k)), wrap="word", relief="solid", bd=1,
                  bg=field_bg, fg=fg, insertbackground=fg)
    box.pack(fill="both", expand=True, padx=12)
    box.insert("1.0", text)

    if refine is not None:
        head.config(text=f"识别结果（引擎：{engine or '未知'}）· AI 整理中，可先编辑…")
        box.configure(state="normal")

        def worker() -> None:
            try:
                out = refine(text)
            except Exception:  # noqa: BLE001
                out = None

            def apply() -> None:
                if out:
                    box.delete("1.0", "end")
                    box.insert("1.0", out)
                    head.config(text=f"识别结果（引擎：{engine or '未知'} + AI 整理），可直接编辑：")
                else:
                    head.config(text=f"识别结果（引擎：{engine or '未知'}），可直接编辑：")

            try:
                root.after(0, apply)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=worker, daemon=True).start()
    bar = tk.Frame(root, bg=bg)
    bar.pack(fill="x", padx=12, pady=10)

    def ok() -> None:
        result["text"] = box.get("1.0", "end-1c").strip()
        root.destroy()

    tk.Button(bar, text="提取入库", command=ok, bg=accent, fg="white", relief="flat",
              font=("Microsoft YaHei UI", -int(15 * k)), padx=16, pady=5).pack(side="right", padx=(6, 0))
    tk.Button(bar, text="取消", command=root.destroy, bg=btn_bg, fg=btn_fg, relief="flat",
              font=("Microsoft YaHei UI", -int(15 * k)), padx=16, pady=5).pack(side="right")
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return result["text"]

# ---------------------------------------------------------------- 入库管线


def ingest(text: str, db: str, cfg: dict) -> tuple[int, int]:
    """文字 → 消息 → 核心管线 → 待办入库；返回 (提取条数, 实际新增条数)。"""
    from chat2todo.ai import AI
    from chat2todo.core import Store, dedupe, extract_todos
    from chat2todo.models import Message
    from chat2todo.semantic import vector_pass

    now = int(time.time())
    msgs = [Message(timestamp=now, sender_id="snap", sender_name="截图", content=line.strip())
            for line in text.splitlines() if line.strip()]
    if not msgs:
        return 0, 0
    unique = dedupe(msgs, strict=bool(cfg.get("strict_dedupe")))
    todos = extract_todos(unique, session="snap")
    todos, _ = vector_pass(todos, cfg)
    ai = AI(cfg)
    if ai.on("review"):
        todos = [t for t in todos if ai.review(t.text) is not False]
    store = Store(db)
    try:
        store.add_messages(unique, session="snap")
        added = store.add_todos(todos)
    finally:
        store.close()
    return len(todos), added

# ---------------------------------------------------------------- 主流程


def _load_cfg() -> dict:
    from chat2todo.config import load

    return load(str(CONFIG) if CONFIG.exists() else None)


def _notify(kind: str, msg: str) -> None:
    print(msg)
    try:
        from tkinter import messagebox

        {"info": messagebox.showinfo, "warn": messagebox.showwarning,
         "error": messagebox.showerror}[kind]("截图待办", msg)
    except Exception:  # noqa: BLE001 - 无显示环境时只打印
        pass


_CJK_SPACED = re.compile(r"(?<=[一-鿿㐀-䶿，。、：；！？])\s+(?=[一-鿿㐀-䶿])")


def _normalize_text(text: str) -> str:
    """WinRT 常在中文字符间插空格（记 得 明 天…），去掉 CJK 之间的空白。"""
    return "\n".join(_CJK_SPACED.sub("", line).strip() for line in text.splitlines()).strip()


def _windows_ocr(dib: bytes) -> str:
    fd, tmp = tempfile.mkstemp(suffix=".bmp")
    os.close(fd)
    bmp = Path(tmp)
    bmp.write_bytes(dib_to_bmp(dib))
    try:
        return _normalize_text(ocr_windows(str(bmp)))
    finally:
        bmp.unlink(missing_ok=True)


def recognize(dib: bytes, cfg: dict, engine: str) -> tuple[str, str]:
    """识别一帧 DIB，返回 (文字, 引擎名)。

    默认 auto：**先用 Windows OCR**（快、对界面文字准），认不出足够文字时才用视觉模型；
    视觉模型在 CPU 上很慢，不适合当默认第一选择。
    """
    model = vision_model_available(cfg) if engine in ("auto", "vision") else ""
    if engine == "vision" and not model:
        raise RuntimeError("配置了强制 vision 但未找到可用视觉模型（检查 [snap] vision_model）")

    if engine == "win":
        return _windows_ocr(dib), "windows-ocr"
    if engine == "vision":
        return _normalize_text(ocr_vision(dib_to_png(dib), cfg, model)), f"vision:{model}"

    # auto：Windows OCR 优先
    try:
        text = _windows_ocr(dib)
    except Exception:  # noqa: BLE001
        text = ""
    if len(text.strip()) >= 4:
        return text, "windows-ocr"
    if model:
        return _normalize_text(ocr_vision(dib_to_png(dib), cfg, model)), f"vision:{model}"
    return text, "windows-ocr"


def _grab_rect(x: int, y: int, w: int, h: int):
    """抓取屏幕矩形，返回 32bpp 自顶向下 DIB（alpha 强制不透明）。"""
    try:
        import ctypes
        from ctypes import wintypes

        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        hdc = u.GetDC(0)
        mem = g.CreateCompatibleDC(hdc)
        bmp = g.CreateCompatibleBitmap(hdc, w, h)
        g.SelectObject(mem, bmp)
        g.BitBlt(mem, 0, 0, w, h, hdc, x, y, 0x00CC0020)      # SRCCOPY
        size = w * h * 4
        buf = ctypes.create_string_buffer(size)

        class BIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        bi = BIH()
        bi.biSize, bi.biWidth, bi.biHeight = 40, w, -h
        bi.biPlanes, bi.biBitCount, bi.biCompression, bi.biSizeImage = 1, 32, 0, size
        g.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(0, hdc)

        data = bytearray(buf.raw)
        for i in range(3, len(data), 4):        # 屏幕抓图 alpha 常为 0，强制不透明
            data[i] = 255
        header = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, size, 0, 0, 0, 0)
        return header + bytes(data)
    except Exception:  # noqa: BLE001
        return None


def capture_region():
    """全屏遮罩里拖框选区，返回该区域 DIB；Esc/取消返回 None。"""
    import tkinter as tk

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.attributes("-alpha", 0.30)
        root.attributes("-transparentcolor", "")
    except Exception:  # noqa: BLE001
        pass
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{sw}x{sh}+0+0")
    cv = tk.Canvas(root, bg="#111111", highlightthickness=0, cursor="crosshair")
    cv.pack(fill="both", expand=True)
    cv.create_text(24, 24, text="拖动框选要识别的区域　·　右键 / Esc 取消", anchor="w",
                   fill="#e8e8e8", font=("Microsoft YaHei UI", 10))
    st = {"x0": 0, "y0": 0, "box": None, "pos": None}

    def press(e):
        if st["box"]:
            cv.delete(st["box"])
        st["x0"], st["y0"] = e.x_root, e.y_root
        st["box"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#4cc2ff", width=2)

    def drag(e):
        if st["box"]:
            cv.coords(st["box"], st["x0"], st["y0"], e.x_root, e.y_root)

    def release(e):
        x1, y1 = e.x_root, e.y_root
        x, y = min(st["x0"], x1), min(st["y0"], y1)
        w, h = abs(x1 - st["x0"]), abs(y1 - st["y0"])
        root.destroy()
        if w >= 8 and h >= 8:
            st["pos"] = (x, y, w, h)

    cv.bind("<Button-1>", press)
    cv.bind("<B1-Motion>", drag)
    cv.bind("<ButtonRelease-1>", release)
    cv.bind("<Button-3>", lambda _e: root.destroy())      # 右键退出截图
    root.bind("<Button-3>", lambda _e: root.destroy())
    root.bind("<Escape>", lambda _e: root.destroy())
    root.mainloop()
    if not st["pos"]:
        return None
    return _grab_rect(*st["pos"])


def ai_refine(text: str, cfg: dict) -> tuple[str, str]:
    """可选：把 OCR 文字交给本地/云端模型整理。返回 (文本, 说明)。失败原样返回。"""
    if not text.strip():
        return text, ""
    try:
        if str((cfg.get("snap") or {}).get("ai_refine", "true")).lower() == "false":
            return text, ""
        from chat2todo.ai import AI

        ai = AI(cfg)
        if not ai.usable:
            return text, ""
        out = ai.refine_ocr(text)
        if out and len(out.strip()) >= 4:
            return out, " + AI整理"
    except Exception:  # noqa: BLE001
        pass
    return text, ""


def capture_once(args, cfg: dict) -> int:
    """热键触发的正流程：框选 → 识别 → 确认 → 入库。"""
    _dbg("弹窗流程开始")
    dib = capture_region()
    if not dib:
        return 0
    _dbg("已抓到选区")
    text, engine = recognize(dib, cfg, args.engine)
    _dbg(f"OCR 完成（{engine}）")
    if not text:
        _notify("warn", f"识别完成（{engine}），但没有认出文字。")
        return 1

    def _refine(t: str):
        out, _note = ai_refine(t, cfg)
        return out if out != t else None

    enabled = str((cfg.get("snap") or {}).get("ai_refine", "true")).lower() != "false"
    _dbg("准备显示确认窗")
    confirmed = confirm_dialog(text, engine, refine=_refine if enabled else None)
    _dbg("确认窗已结束")
    if not confirmed:
        return 0
    total, added = ingest(confirmed, args.db, cfg)
    _notify("info", f"已入库：新增 {added} 条待办（本次提取 {total} 条，引擎 {engine}）。")
    return 0


def snap_once(args, cfg: dict) -> int:
    dib = clipboard_dib()
    if not dib:
        _notify("info", "剪贴板里没有图片。\n请先用 Win+Shift+S 框选截图，再点本功能。")
        return 1
    text, engine = recognize(dib, cfg, args.engine)
    if not text:
        _notify("warn", f"识别完成（{engine}），但没有认出文字。")
        return 1
    confirmed = confirm_dialog(text, engine)
    if not confirmed:
        print("已取消")
        return 0
    total, added = ingest(confirmed, args.db, cfg)
    _notify("info", f"已入库：新增 {added} 条待办（本次提取 {total} 条，引擎 {engine}）。")
    return 0


def watch_loop(args, cfg: dict) -> int:
    """监视剪贴板：每截一张图自动识别并弹确认窗，Ctrl+C 退出。"""
    u, _ = _win32()
    last = u.GetClipboardSequenceNumber()
    print(f"开始监视剪贴板（每 {args.interval}s 检查一次），Ctrl+C 退出")
    try:
        while True:
            time.sleep(args.interval)
            seq = u.GetClipboardSequenceNumber()
            if seq == last:
                continue
            last = seq
            dib = clipboard_dib()
            if not dib:
                continue
            print("检测到新截图，识别中…")
            try:
                snap_once(args, cfg)
            except Exception as exc:  # noqa: BLE001
                _notify("error", f"本次识别失败：{exc}")
    except KeyboardInterrupt:
        print("已停止监视")
        return 0


def _single_instance(name: str = "Chat2TodoSnapHotkey") -> bool:
    """热键常驻模式单实例；重复启动直接退出，避免抢同一个组合键。"""
    try:
        import ctypes

        ctypes.windll.kernel32.CreateMutexW(None, False, name)
        return ctypes.windll.kernel32.GetLastError() != 183
    except Exception:  # noqa: BLE001
        return True


def hotkey_loop(args, cfg: dict) -> int:
    """常驻模式：注册全局热键（默认 Ctrl+Alt+S），按下即唤出截图待办。"""
    if not _single_instance():
        print("截图热键已在运行")
        return 0
    u, _ = _win32()
    u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                            wintypes.UINT, wintypes.UINT]
    spec = str((cfg.get("snap") or {}).get("hotkey") or "ctrl+alt+s")
    parsed = None
    try:
        from chat2todo.core.hotkey import parse_hotkey

        parsed = parse_hotkey(spec)
    except Exception:  # noqa: BLE001
        parsed = None
    if parsed is None:
        print(f"[E4009] 无法识别快捷键配置：{spec}，回退到 ctrl+alt+s")
        spec = "ctrl+alt+s"
        from chat2todo.core.hotkey import parse_hotkey as _p

        parsed = _p(spec)
    if not u.RegisterHotKey(None, 1, parsed[0] & ~0x4000, parsed[1]):
        _notify("error", f"全局热键 {spec} 被其它程序占用，请到设置里换一个组合")
        return 2
    print(f"截图热键已生效：{spec}（结束本进程即取消）")
    msg = wintypes.MSG()
    try:
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:                               # WM_HOTKEY
                # 直接在本进程里截屏，省掉每次冷启动一个解释器（约 1-2 秒）
                try:
                    capture_once(args, cfg)
                except Exception as exc:  # noqa: BLE001
                    _notify("error", f"截图处理失败：{exc}")
    finally:
        u.UnregisterHotKey(None, 1)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="截图待办：剪贴板图片 → 识别 → 确认 → 入库")
    ap.add_argument("--db", default="", help="目标数据库（默认 %%USERPROFILE%%\\Chat2Todo\\qq.db）")
    ap.add_argument("--capture", action="store_true", help="框选屏幕区域识别（热键默认走这条）")
    ap.add_argument("--watch", action="store_true", help="持续监视剪贴板，而非只处理一次")
    ap.add_argument("--hotkey", action="store_true", help="常驻并注册全局热键（默认 Ctrl+Alt+S，可在设置里改）")
    ap.add_argument("--interval", type=float, default=2.0, help="--watch 的轮询间隔（秒）")
    ap.add_argument("--engine", choices=["auto", "win", "vision"], default="auto",
                    help="识别引擎：auto=视觉模型可用则用，否则 Windows OCR")
    args = ap.parse_args()
    _dpi_aware()

    cfg = _load_cfg()
    if not args.db:
        args.db = str(ROOT / "qq.db")     # 与控制台/看板同一个库

    try:
        if args.hotkey:
            return hotkey_loop(args, cfg)
        if args.capture:
            return capture_once(args, cfg)
        return watch_loop(args, cfg) if args.watch else snap_once(args, cfg)
    except Exception as exc:  # noqa: BLE001
        _notify("error", f"截图待办失败：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
