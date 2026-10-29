"""桌面美化联动：脱敏待办 JSON/ICS 输出 + 本地回写接口。

文档口径（逐条落地）：
  * 一键生成待办 JSON / ICS 输出文件，供 DeskBox 等美化工具的小组件读取；
  * 输出内容默认脱敏（白名单群与归档口径一致）；
  * 待办变更做 1~2 秒防抖合并，内容没变就不写盘，避免频繁磁盘写入；
  * 本地服务只监听 127.0.0.1，随机端口；错误 token 直接 401 并记日志；
  * token 写在输出目录的 ``token.txt``，Windows 上用 icacls 收紧 ACL；
  * 小组件勾选 / 编辑 / 删除 → 带 token 请求本地接口 → 回写 SQLite。

本模块只做「输出 + 接口」，不启动线程循环；Qt 侧把 ``DesktopLink`` 挂到
待办变更信号上即可，``start_server`` 与 ``stop_server`` 负责生命周期。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .store import Store
from .privacy import redact_for_archive
from .. import __version__

log = logging.getLogger("chat2todo.desktop")

JSON_NAME = "todos.json"
ICS_NAME = "todos.ics"
TOKEN_NAME = "token.txt"
ENDPOINT_NAME = "endpoint.json"

_TOKEN_BYTES = 24
_MAX_BODY = 8 * 1024


# ---------------------------------------------------------------------------
# 输出目录 / token
# ---------------------------------------------------------------------------

def default_out_dir(db_path: str) -> str:
    """默认放在库文件同目录的 desktop/ 下，跟着用户选的数据目录走。"""
    base = os.path.dirname(os.path.abspath(db_path)) if db_path else os.getcwd()
    return os.path.join(base, "desktop")


def out_dir(cfg: Dict[str, Any], db_path: str) -> str:
    desktop = (cfg or {}).get("desktop") or {}
    picked = str(desktop.get("dir") or "").strip()
    return picked or default_out_dir(db_path)


def token_path(folder: str) -> str:
    return os.path.join(folder, TOKEN_NAME)


def _restrict_acl(path: str) -> None:
    """Windows 下把 token 文件权限收紧到当前用户，失败只记 debug。"""
    if sys.platform != "win32":
        return
    user = os.environ.get("USERNAME") or ""
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
            check=False, capture_output=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        log.debug("收紧 token 文件 ACL 失败（不影响使用）", exc_info=True)


def load_token(folder: str) -> str:
    """读已有 token；文件不存在返回空串（不在这里生成，避免只读场景落盘）。"""
    try:
        with open(token_path(folder), "r", encoding="ascii") as fh:
            return fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def ensure_token(folder: str, rotate: bool = False) -> str:
    """取 token，不存在或要求轮换时重新生成并写盘。"""
    os.makedirs(folder, exist_ok=True)
    token = "" if rotate else load_token(folder)
    if token:
        return token
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    tmp = token_path(folder) + ".tmp"
    with open(tmp, "w", encoding="ascii", newline="\n") as fh:
        fh.write(token + "\n")
    os.replace(tmp, token_path(folder))
    _restrict_acl(token_path(folder))
    return token


# ---------------------------------------------------------------------------
# 待办 → 脱敏数据
# ---------------------------------------------------------------------------

def _due_ts(todo) -> Optional[int]:
    from .deadline import parse as parse_due

    try:
        return parse_due(todo.text or "")
    except Exception:  # noqa: BLE001 - 解析失败就当没有截止日
        return None


def export_items(store, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把库里的待办渲染成脱敏字典列表（供 JSON 与接口共用）。"""
    items: List[Dict[str, Any]] = []
    for todo in store.list_todos(None):
        if todo.status in ("noise", "muted", "deleted"):
            continue
        session = todo.session or ""
        text, _hits = redact_for_archive(todo.text or "", session, cfg)
        due = _due_ts(todo)
        items.append({
            "id": int(todo.todo_id),
            "text": text,
            "session": session,
            "sender": todo.sender or "",
            "status": todo.status,
            "confidence": round(float(todo.confidence or 0.0), 2),
            "created": int(todo.message_ts or 0),
            "due": int(due) if due else 0,
        })
    return items


def build_json(items: List[Dict[str, Any]]) -> str:
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "version": __version__,
        "count": len(items),
        "todos": items,
    }
    return json.dumps(payload, ensure_ascii=False, indent=1) + "\n"


# ---------------------------------------------------------------------------
# ICS
# ---------------------------------------------------------------------------

_ICS_STATUS = {
    "done": ("COMPLETED", "已完成"),
    "pending": ("NEEDS-ACTION", "待办"),
    "unconfirmed": ("TENTATIVE", "待确认"),
}


def _ics_escape(text: str) -> str:
    return (str(text or "")
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\r\n", "\\n")
            .replace("\n", "\\n"))


def _fold(line: str, limit: int = 75) -> List[str]:
    """按 UTF-8 字节折行，续行前加一个空格（RFC 5545）。"""
    out: List[str] = []
    cur, size = "", 0
    for ch in line:
        width = len(ch.encode("utf-8"))
        if size + width > limit:
            out.append(cur)
            cur, size = " " + ch, 1 + width
        else:
            cur += ch
            size += width
    out.append(cur)
    return out


def build_ics(items: List[Dict[str, Any]]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Chat2Todo//Desktop Link//CN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Chat2Todo 待办",
    ]
    for item in items:
        due = int(item.get("due") or 0)
        if due <= 0:
            continue
        try:
            day = datetime.fromtimestamp(due).strftime("%Y%m%d")
        except (OverflowError, OSError, ValueError):
            continue
        status, status_cn = _ICS_STATUS.get(str(item.get("status")), ("NEEDS-ACTION", "待办"))
        desc = (f"群：{item.get('session') or '未知'}\n"
                f"发送者：{item.get('sender') or '未知'}\n"
                f"状态：{status_cn}")
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:c2t-{item['id']}@chat2todo.local",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{day}",
            f"SUMMARY:{_ics_escape(item.get('text') or '')}",
            f"DESCRIPTION:{_ics_escape(desc)}",
            f"STATUS:{status}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    folded: List[str] = []
    for line in lines:
        folded.extend(_fold(line))
    return "\r\n".join(folded) + "\r\n"


# ---------------------------------------------------------------------------
# 落盘（防抖 / 内容未变不写）
# ---------------------------------------------------------------------------

def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


class DesktopLink:
    """一个进程一份：负责输出文件与本地回写服务。"""

    def __init__(self, cfg: Dict[str, Any], db_path: str):
        self.cfg = cfg or {}
        self.db_path = db_path
        desktop = self.cfg.get("desktop") or {}
        self.folder = out_dir(self.cfg, db_path)
        self.debounce = max(1.0, min(2.0, float(desktop.get("debounce_ms", 1500)) / 1000.0))
        self.want_json = bool(desktop.get("json", True))
        self.want_ics = bool(desktop.get("ics", True))
        self.port = int(desktop.get("port") or 0)
        self.token = ""
        self.url = ""
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._last: Dict[str, str] = {}
        self.on_change = None          # Qt 侧可以挂「回写后刷新看板」
        self._closed = False

    # ---------- 输出 ----------

    @property
    def enabled(self) -> bool:
        return bool((self.cfg.get("desktop") or {}).get("enabled"))

    def _store(self) -> Store:
        return Store(self.db_path)

    def snapshot(self) -> List[Dict[str, Any]]:
        store = self._store()
        try:
            return export_items(store, self.cfg)
        finally:
            store.close()

    def export(self) -> bool:
        """立即写 JSON / ICS；内容与上次相同则一个字节都不写。"""
        if not self.enabled:
            return False
        try:
            os.makedirs(self.folder, exist_ok=True)
        except OSError as exc:
            log.warning("创建桌面联动输出目录失败：%s", exc)
            return False
        try:
            items = self.snapshot()
        except Exception:  # noqa: BLE001 - 库暂时打不开不能拖垮主流程
            log.warning("读取待办失败，本次不输出", exc_info=True)
            return False
        changed = False
        with self._lock:
            # 摘要只按待办数据算：JSON 里的生成时间 / ICS 里的 DTSTAMP 每次都不同，
            # 拿整段文本当摘要会导致「内容没变也天天写盘」。
            key = _digest(json.dumps(items, ensure_ascii=False, sort_keys=True))
            targets: List[Tuple[str, str]] = []
            if self.want_json:
                targets.append((JSON_NAME, build_json(items)))
            if self.want_ics:
                targets.append((ICS_NAME, build_ics(items)))
            for name, text in targets:
                if self._last.get(name) == key:
                    continue
                try:
                    _atomic_write(os.path.join(self.folder, name), text)
                except OSError as exc:
                    log.warning("写 %s 失败：%s", name, exc)
                    continue
                self._last[name] = key
                changed = True
        return changed

    def schedule(self, delay: Optional[float] = None) -> None:
        """1~2 秒防抖：期间多次变更只落一次盘。"""
        if not self.enabled or self._closed:
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(delay if delay is not None else self.debounce,
                                          self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        try:
            self.export()
        except Exception:  # noqa: BLE001
            log.warning("桌面联动输出失败", exc_info=True)

    # ---------- 本地服务 ----------

    def start_server(self) -> str:
        """监听 127.0.0.1 并写 endpoint.json；失败返回空串（功能降级不报错）。"""
        if not self.enabled or self._server is not None:
            return self.url
        try:
            os.makedirs(self.folder, exist_ok=True)
            self.token = ensure_token(self.folder)
        except OSError as exc:
            log.warning("初始化桌面联动 token 失败：%s", exc)
            return ""
        handler = type("_LinkHandler", (_LinkHandler,), {"link": self})
        try:
            server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        except OSError:
            log.warning("端口 %s 不可用，改用随机端口", self.port)
            try:
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            except OSError as exc:
                log.warning("桌面联动服务启动失败：%s", exc)
                return ""
        server.daemon_threads = True
        self._server = server
        port = server.server_address[1]
        self.url = f"http://127.0.0.1:{port}"
        try:
            _atomic_write(
                os.path.join(self.folder, ENDPOINT_NAME),
                json.dumps({
                    "url": self.url,
                    "token_file": TOKEN_NAME,
                    "endpoints": {
                        "get": "/todos",
                        "done": "/done",
                        "pending": "/pending",
                        "edit": "/edit",
                        "delete": "/delete",
                    },
                }, ensure_ascii=False, indent=1) + "\n",
            )
        except OSError as exc:
            log.warning("写 endpoint.json 失败：%s", exc)
        self._thread = threading.Thread(target=server.serve_forever,
                                       name="c2t-desktop-link", daemon=True)
        self._thread.start()
        log.info("桌面联动接口已启动：%s", self.url)
        return self.url

    def rotate_token(self) -> str:
        """手动轮换 token（设置页按钮 / CLI），旧 token 立即失效。"""
        try:
            self.token = ensure_token(self.folder, rotate=True)
        except OSError as exc:
            log.warning("轮换 token 失败：%s", exc)
            return ""
        return self.token

    def stop_server(self) -> None:
        self._closed = True
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.url = ""

    # ---------- 回写 ----------

    def apply(self, action: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
        """处理一次小组件操作；返回 (是否成功, 说明)。"""
        try:
            todo_id = int(payload.get("id"))
        except (TypeError, ValueError):
            return False, "缺少合法的待办编号"
        store = self._store()
        try:
            if action == "done":
                ok = store.set_status(todo_id, "done")
            elif action == "pending":
                ok = store.set_status(todo_id, "pending")
            elif action == "edit":
                text = str(payload.get("text") or "").strip()
                if not text:
                    return False, "待办内容不能为空"
                ok = store.update_text(todo_id, text)
            elif action == "delete":
                ok = store.trash_item(todo_id) is not None
            else:
                return False, f"未知操作 {action}"
        except Exception as exc:  # noqa: BLE001 - 单次回写失败不影响服务
            log.warning("桌面联动回写失败：%s", exc)
            return False, "回写失败"
        finally:
            store.close()
        if not ok:
            return False, f"没有编号为 {todo_id} 的待办"
        self.schedule()
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                log.debug("桌面联动变更回调失败", exc_info=True)
        return True, "ok"


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------

class _LinkHandler(BaseHTTPRequestHandler):
    link: DesktopLink = None  # type: ignore[assignment] - 由 start_server 注入
    server_version = "Chat2TodoDesktopLink"
    protocol_version = "HTTP/1.1"

    # ---------- 校验 ----------

    def _host_ok(self) -> bool:
        """只认可回环地址的 Host，挡掉 DNS rebinding。"""
        host = str(self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}",
                        "127.0.0.1", "localhost")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = self.server.server_address[1]
        parsed = urlparse(origin)
        return (parsed.scheme == "http"
                and parsed.hostname in ("127.0.0.1", "localhost")
                and (parsed.port in (None, port)))

    def _token_ok(self) -> bool:
        supplied = ""
        header = str(self.headers.get("Authorization") or "")
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()
        if not supplied:
            query = urlparse(self.path).query
            for part in query.split("&"):
                if part.startswith("token="):
                    supplied = part[6:]
                    break
        expected = self.link.token or ""
        return bool(expected) and hmac.compare_digest(supplied, expected)

    def _send(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _guard(self) -> bool:
        if not self._host_ok():
            log.warning("桌面联动拒绝异常 Host：%s", self.headers.get("Host"))
            self._send(403, {"ok": False, "error": "Host 不被允许"})
            return False
        if not self._origin_ok():
            log.warning("桌面联动拒绝异常 Origin：%s", self.headers.get("Origin"))
            self._send(403, {"ok": False, "error": "Origin 不被允许"})
            return False
        if not self._token_ok():
            log.warning("桌面联动 token 校验失败：%s %s", self.command, self.path)
            self._send(401, {"ok": False, "error": "token 无效"})
            return False
        return True

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if not self._guard():
            return
        path = urlparse(self.path).path.rstrip("/") or "/todos"
        if path in ("/todos", "/"):
            try:
                items = self.link.snapshot()
            except Exception:  # noqa: BLE001
                self._send(500, {"ok": False, "error": "读取待办失败"})
                return
            self._send(200, {"ok": True, "count": len(items), "todos": items})
            return
        if path == "/status":
            try:
                number = len(self.link.snapshot())
            except Exception:  # noqa: BLE001
                number = -1
            self._send(200, {"ok": True, "version": __version__,
                             "count": number, "url": self.link.url})
            return
        self._send(404, {"ok": False, "error": "未知路径"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        length = 0
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > _MAX_BODY:
            self._send(413, {"ok": False, "error": "请求体过大"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            self._send(400, {"ok": False, "error": "请求体不是合法 JSON"})
            return
        if not isinstance(payload, dict):
            self._send(400, {"ok": False, "error": "请求体必须是对象"})
            return
        path = urlparse(self.path).path.rstrip("/")
        action = path.lstrip("/")
        if action not in ("done", "pending", "edit", "delete"):
            self._send(404, {"ok": False, "error": "未知操作"})
            return
        ok, msg = self.link.apply(action, payload)
        self._send(200 if ok else 400, {"ok": ok, "message": msg})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # 默认实现会往 stderr 打访问日志，这里转到 logging 便于随程序一起落盘
        log.debug("desktop-link %s - %s", self.address_string(), fmt % args)


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------

def make_link(cfg: Dict[str, Any], db_path: str) -> Optional[DesktopLink]:
    """按配置建联动对象；未启用或没有库路径时返回 None。"""
    if not db_path:
        return None
    link = DesktopLink(cfg, db_path)
    return link if link.enabled else None
