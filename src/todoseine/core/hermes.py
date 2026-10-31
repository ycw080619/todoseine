"""Hermes 运维服务：本机 token 鉴权 + 巡检 + 每日汇总 + 指令响应。

文档口径（逐条落地）：
  * 一次性随机 token，存加密配置（Windows 用 DPAPI），设置页可一键复制 / 手动轮换；
  * 只监听 127.0.0.1，错误 token 一律 401 并记日志；
  * 指令响应：接收带 token 的本地指令，调用宿主 API 执行对应操作；
  * 转发插件未启用时返回「功能未启用」，不做半自动转发；
  * 定时巡检：采集进程 / AI 服务状态，异常自动重启，服务异常不影响主程序基础功能；
  * 每日汇总：生成当日消息摘要与待办清单。

本模块不自己起循环；Qt / CLI 负责把宿主能力（host）注入进来。
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .backup import dpapi_protect, dpapi_unprotect
from .privacy import redact_for_archive

log = logging.getLogger("todoseine.hermes")

TOKEN_NAME = "token.bin"
_MAGIC = b"C2THERMES1\n"
_MAX_BODY = 8 * 1024
_TOKEN_BYTES = 24


# ---------------------------------------------------------------------------
# token（DPAPI 加密存储）
# ---------------------------------------------------------------------------

def hermes_dir(home: Optional[str] = None) -> str:
    base = home or os.path.join(os.path.expanduser("~"), ".chat2todo")
    return os.path.join(base, "hermes")


def token_path(folder: Optional[str] = None) -> str:
    return os.path.join(folder or hermes_dir(), TOKEN_NAME)


def load_token(folder: Optional[str] = None) -> str:
    try:
        with open(token_path(folder), "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    if raw.startswith(_MAGIC):
        plain = dpapi_unprotect(raw[len(_MAGIC):])
        if plain is None:
            log.warning("Hermes token 解密失败（可能换了 Windows 用户），请轮换")
            return ""
        try:
            return plain.decode("ascii").strip()
        except UnicodeDecodeError:
            return ""
    try:
        return raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return ""


def ensure_token(folder: Optional[str] = None, rotate: bool = False) -> str:
    """取 token；不存在或要求轮换时生成并加密写盘（DPAPI 不可用则明文）。"""
    target = folder or hermes_dir()
    os.makedirs(target, exist_ok=True)
    if not rotate:
        existing = load_token(target)
        if existing:
            return existing
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    encrypted = dpapi_protect(token.encode("ascii"))
    payload = (_MAGIC + encrypted) if encrypted else token.encode("ascii")
    tmp = token_path(target) + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(payload)
    os.replace(tmp, token_path(target))
    try:
        os.chmod(token_path(target), 0o600)
    except OSError:
        pass
    return token


# ---------------------------------------------------------------------------
# 巡检 / 每日汇总
# ---------------------------------------------------------------------------

def inspect(host) -> Dict[str, Any]:
    """巡检一次：采集是否在跑、AI 配置是否完整；异常时请求宿主自动重启。"""
    report: Dict[str, Any] = {"issues": [], "restarted": False, "at": datetime.now().isoformat(timespec="seconds")}
    try:
        status = host.status()
    except Exception as exc:  # noqa: BLE001 - 巡检自身不能把服务带崩
        report["issues"].append(f"读取状态失败：{exc}")
        status = {}
    report["status"] = status

    running = bool(status.get("running", True))
    if not running:
        report["issues"].append("采集未在运行")
        try:
            report["restarted"] = bool(host.restart())
        except Exception as exc:  # noqa: BLE001
            report["issues"].append(f"自动重启失败：{exc}")
        if report["restarted"]:
            report["issues"].append("已自动重启采集")

    if not status.get("ai_configured", True):
        report["issues"].append("AI 已启用但 base_url / api_key 不完整，当前走关键词规则")
    return report


def daily_summary(store, cfg: Dict[str, Any], day: Optional[datetime] = None) -> str:
    """生成当日汇总（消息数 / 待办清单），全部按归档口径脱敏。"""
    day = day or datetime.now()
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    since = int(start.timestamp())
    stats = store.stats(since)

    groups: Dict[str, int] = {}
    for todo in store.list_todos(None):
        if int(todo.message_ts or 0) < since:
            continue
        if todo.status in ("noise", "muted"):
            continue
        groups[todo.session or "未标记"] = groups.get(todo.session or "未标记", 0) + 1

    lines = [
        f"# TodoSeine 每日汇总 · {start.strftime('%Y-%m-%d')}",
        "",
        f"- 新消息：{stats['messages']} 条",
        f"- 新待办：{stats['todos']} 条（待处理 {stats['pending']}，已完成 {stats['done']}）",
        "",
    ]
    if groups:
        lines.append("## 按来源")
        for name, count in sorted(groups.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {name}：{count} 条")
        lines.append("")
    pending = [t for t in store.list_todos("pending") if int(t.message_ts or 0) >= since]
    if pending:
        lines.append("## 今日待办")
        for todo in pending[:30]:
            text, _hits = redact_for_archive(todo.text or "", todo.session or "", cfg)
            where = f"{todo.session or ''} · {todo.sender or ''}".strip(" ·")
            lines.append(f"- [ ] {text}" + (f"（{where}）" if where else ""))
    else:
        lines.append("## 今日待办")
        lines.append("- 没有新的待处理条目")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

class HermesService:
    """一个进程一份；host 由主程序注入（Qt 传闭包，CLI 用内存实现）。"""

    def __init__(self, host, folder: Optional[str] = None, port: int = 0):
        self.host = host
        self.folder = folder or hermes_dir()
        self.port = int(port or 0)
        self.token = ""
        self.url = ""
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def rotate_token(self) -> str:
        self.token = ensure_token(self.folder, rotate=True)
        return self.token

    def start(self) -> str:
        if self._server is not None:
            return self.url
        try:
            self.token = ensure_token(self.folder)
        except OSError as exc:
            log.warning("初始化 Hermes token 失败：%s", exc)
            return ""
        handler = type("_HermesHandler", (_HermesHandler,), {"service": self})
        try:
            server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        except OSError:
            try:
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            except OSError as exc:
                log.warning("Hermes 服务启动失败：%s", exc)
                return ""
        server.daemon_threads = True
        self._server = server
        self.url = f"http://127.0.0.1:{server.server_address[1]}"
        self._thread = threading.Thread(target=server.serve_forever,
                                        name="c2t-hermes", daemon=True)
        self._thread.start()
        log.info("Hermes 服务已启动：%s", self.url)
        return self.url

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.url = ""

    # ---------- 指令 ----------

    def dispatch(self, payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        command = str(payload.get("command") or payload.get("cmd") or "").strip()
        if not command:
            return False, {"error": "缺少 command"}
        try:
            if command == "status":
                return True, {"status": self.host.status()}
            if command == "inspect":
                return True, inspect(self.host)
            if command == "restart":
                return bool(self.host.restart()), {"restarted": True}
            if command == "todos":
                return True, {"todos": self.host.snapshot()}
            if command == "summary":
                return True, {"summary": self.host.summary()}
            if command == "add":
                text = str(payload.get("text") or "").strip()
                if not text:
                    return False, {"error": "待办内容不能为空"}
                return self.host.apply("add", {"text": text})
            if command in ("done", "pending", "edit", "delete"):
                return self.host.apply(command, payload)
            if command == "forward":
                if not self.host.forward_enabled():
                    return False, {"error": "功能未启用：半自动转发插件未启用"}
                return False, {"error": "功能未启用：当前版本不提供自动发送"}
        except Exception as exc:  # noqa: BLE001 - 服务异常不影响主程序
            log.warning("Hermes 指令执行失败：%s", exc)
            return False, {"error": f"执行失败：{type(exc).__name__}"}
        return False, {"error": f"未知指令 {command}"}


class _HermesHandler(BaseHTTPRequestHandler):
    service: HermesService = None  # type: ignore[assignment]
    server_version = "TodoSeineHermes"
    protocol_version = "HTTP/1.1"

    def _host_ok(self) -> bool:
        host = str(self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}",
                        "127.0.0.1", "localhost")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        port = self.server.server_address[1]
        return (parsed.scheme == "http"
                and parsed.hostname in ("127.0.0.1", "localhost")
                and parsed.port in (None, port))

    def _token_ok(self) -> bool:
        supplied = ""
        header = str(self.headers.get("Authorization") or "")
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()
        if not supplied:
            for part in urlparse(self.path).query.split("&"):
                if part.startswith("token="):
                    supplied = part[6:]
                    break
        expected = self.service.token or ""
        return bool(expected) and hmac.compare_digest(supplied, expected)

    def _send(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps({"ok": code < 400, **payload}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _guard(self) -> bool:
        if not self._host_ok() or not self._origin_ok():
            log.warning("Hermes 拒绝异常 Host/Origin：%s %s",
                        self.headers.get("Host"), self.headers.get("Origin"))
            self._send(403, {"error": "Host/Origin 不被允许"})
            return False
        if not self._token_ok():
            log.warning("Hermes token 校验失败：%s %s", self.command, self.path)
            self._send(401, {"error": "token 无效"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard():
            return
        try:
            self._route_get()
        except Exception:  # noqa: BLE001 - 单个请求失败也要给调用方一个回复
            log.exception("Hermes GET 处理失败：%s", self.path)
            self._send(500, {"error": "服务内部错误"})

    def _route_get(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        if path in ("", "/", "/status"):
            self._send(200, {"status": self.service.host.status()})
            return
        if path == "/todos":
            self._send(200, {"todos": self.service.host.snapshot()})
            return
        if path == "/summary":
            self._send(200, {"summary": self.service.host.summary()})
            return
        self._send(404, {"error": "未知路径"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        try:
            self._route_post()
        except Exception:  # noqa: BLE001
            log.exception("Hermes POST 处理失败：%s", self.path)
            self._send(500, {"error": "服务内部错误"})

    def _route_post(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        if path not in ("", "/command"):
            self._send(404, {"error": "未知路径"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > _MAX_BODY:
            self._send(413, {"error": "请求体过大"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            self._send(400, {"error": "请求体不是合法 JSON"})
            return
        if not isinstance(payload, dict):
            self._send(400, {"error": "请求体必须是对象"})
            return
        ok, result = self.service.dispatch(payload)
        self._send(200 if ok else 400, result)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug("hermes %s - %s", self.address_string(), fmt % args)


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------

class MemoryHost:
    """CLI / 测试用的最小宿主：每次调用自己开 Store。

    HTTP 请求跑在独立线程里，而 SQLite 连接不能跨线程复用，
    所以这里只存库路径，每次操作现开现关。
    """

    def __init__(self, db_path: str, cfg: Dict[str, Any], status_fn=None):
        from .store import Store

        self._store_cls = Store
        self.db_path = db_path
        self.cfg = cfg
        self._status_fn = status_fn
        self.forward = bool((cfg.get("forward") or {}).get("enabled", False))

    def status(self) -> Dict[str, Any]:
        if self._status_fn is not None:
            return self._status_fn()
        store = self._store_cls(self.db_path)
        try:
            stats = store.stats()
        finally:
            store.close()
        ai = self.cfg.get("ai") or {}
        return {
            "running": True,
            "messages": stats["messages"],
            "pending": stats["pending"],
            "ai_enabled": bool(ai.get("enabled")),
            "ai_configured": bool(ai.get("base_url") and ai.get("api_key")),
            "daily_summary": True,
        }

    def snapshot(self) -> List[Dict[str, Any]]:
        from .desktop_link import export_items

        store = self._store_cls(self.db_path)
        try:
            return export_items(store, self.cfg)
        finally:
            store.close()

    def summary(self) -> str:
        store = self._store_cls(self.db_path)
        try:
            return daily_summary(store, self.cfg)
        finally:
            store.close()

    def apply(self, action: str, payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        store = self._store_cls(self.db_path)
        try:
            return _apply_on(store, action, payload)
        finally:
            store.close()

    def restart(self) -> bool:
        return True

    def forward_enabled(self) -> bool:
        return self.forward


def _apply_on(store, action: str, payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """在给定 Store 上执行一次回写（Qt 宿主与 MemoryHost 共用）。"""
    try:
        todo_id = int(payload.get("id"))
    except (TypeError, ValueError):
        todo_id = 0
    if action == "add":
        from ..models import TodoItem

        text = str(payload.get("text") or "").strip()
        if not text:
            return False, {"error": "待办内容不能为空"}
        added = store.add_todos([TodoItem(
            text=text, session="Hermes", sender="指令",
            message_ts=int(datetime.now().timestamp()),
            reason="Hermes 指令", confidence=1.0)])
        return (True, {"added": added}) if added else (False, {"error": "已存在相同待办"})
    if not todo_id:
        return False, {"error": "缺少合法的待办编号"}
    if action == "done":
        ok = store.set_status(todo_id, "done")
    elif action == "pending":
        ok = store.set_status(todo_id, "pending")
    elif action == "edit":
        text = str(payload.get("text") or "").strip()
        if not text:
            return False, {"error": "待办内容不能为空"}
        ok = store.update_text(todo_id, text)
    elif action == "delete":
        ok = store.trash_item(todo_id) is not None
    else:
        return False, {"error": f"未知操作 {action}"}
    if not ok:
        return False, {"error": f"没有编号为 {todo_id} 的待办"}
    return True, {"id": todo_id, "action": action}


def tomorrow_start(now: Optional[datetime] = None) -> datetime:
    base = (now or datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0)
    return base + timedelta(days=1)
