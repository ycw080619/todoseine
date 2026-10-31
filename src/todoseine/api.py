"""本地 HTTP API（纯标准库，仅监听 127.0.0.1）。

给「别的工具 / 浏览器页面 / 脚本」提供一个能 curl 的入口，
让待办数据可被外部程序读写，而不是锁死在 tkinter 窗口里。

端点：
    GET  /health              健康检查
    GET  /todos?status=&limit= 待办列表（status: pending / done / noise / 空=全部）
    GET  /stats               统计摘要
    GET  /search?q=&limit=    全文搜索消息
    POST /todos/{id}/done     标记完成（请求体可选 {"status": "pending"|"done"|"noise"}）

安全边界（本地桌面工具，不是公共服务）：
- 只绑 127.0.0.1，并在请求处理时二次校验对端地址，非本地直连一律拒绝
- 请求体上限 64 KB，只接受 JSON
- CORS 只回同源 localhost，不回 ``*``，避免任意网页读取本地数据
"""

from __future__ import annotations

import ipaddress
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .core.store import Store

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY = 64 * 1024
_LOCAL_ADDRS = ("127.0.0.1", "::1", "localhost")
_ALLOWED_STATUS = ("pending", "done", "noise")
_BAD_BODY = object()          # 请求体非法时的哨兵


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _is_local(addr: str) -> bool:
    """判断是否本机。

    不能用 ``startswith("127.")``：``127.0.0.1.evil.com`` 这种域名也满足前缀，
    会被当成回环地址放行 CORS，任意网页就能读走本地待办数据。
    这里只认三种情况：精确的 localhost/::1，或能解析成 IP 且确实是回环地址。
    域名一律不放行（哪怕是 localhost 的变体）。
    """
    if not addr:
        return False
    if addr.lower() in _LOCAL_ADDRS:
        return True
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False   # 域名（含 127.0.0.1.evil.com 这类伪装）一律拒绝


def _cors_origin(origin: str) -> Optional[str]:
    """只放行本机页面，其它来源不回 CORS 头（浏览器会自己拦下）。"""
    if not origin:
        return None
    host = urlparse(origin).hostname or ""
    return origin if _is_local(host) else None


class _ApiHandler(BaseHTTPRequestHandler):
    server_version = "TodoSeineAPI/1.0"
    store_path = ""                # 由 make_handler 覆盖；空则用配置默认库

    # --- 基础工具 ---------------------------------------------
    def log_message(self, fmt, *args):   # 静默：不刷屏到控制台
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send(self, code: int, payload) -> None:
        body = _json_bytes(payload)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = _cors_origin(self.headers.get("Origin", ""))
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _store(self) -> Store:
        if self.store_path:
            return Store(self.store_path)
        from .config import DEFAULTS      # 兜底：与控制台共用一个默认库

        return Store(DEFAULTS["db_path"])

    def _query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    def _int_param(self, params: dict, key: str, default: int) -> int:
        try:
            return max(1, min(500, int(params.get(key, [default])[0])))
        except (TypeError, ValueError):
            return default

    # --- 请求入口 ---------------------------------------------
    def do_OPTIONS(self):        # noqa: N802
        self.send_response(204)
        origin = _cors_origin(self.headers.get("Origin", ""))
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):            # noqa: N802
        if not _is_local(self.client_address[0]):
            self._send(403, {"error": "只接受本机请求"})
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        params = self._query()

        if path == "/health":
            self._send(200, {"ok": True, "service": "todoseine", "version": _version()})
            return
        if path == "/todos":
            self._handle_todos(params)
            return
        if path == "/stats":
            self._handle_stats()
            return
        if path == "/search":
            self._handle_search(params)
            return
        self._send(404, {"error": f"未知端点 {path}"})

    def do_POST(self):           # noqa: N802
        if not _is_local(self.client_address[0]):
            self._send(403, {"error": "只接受本机请求"})
            return
        # 只接受 JSON：跨站页面能用 text/plain 发「简单请求」绕过 CORS 预检，
        # 这里卡掉一切非 JSON 的写操作，浏览器就没法偷偷改/删待办。
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send(415, {"error": "Content-Type 必须是 application/json"})
            return
        path = urlparse(self.path).path.rstrip("/")
        parts = [p for p in path.split("/") if p]

        # /todos/{id}/done
        if len(parts) == 3 and parts[0] == "todos" and parts[2] == "done":
            self._handle_done(parts[1])
            return
        self._send(404, {"error": f"未知端点 {path}"})

    # --- 各端点实现 -------------------------------------------
    def _handle_todos(self, params: dict) -> None:
        status = (params.get("status") or [""])[0].strip()
        if status and status not in _ALLOWED_STATUS:
            self._send(400, {"error": f"status 只能是 {'/'.join(_ALLOWED_STATUS)}"})
            return
        limit = self._int_param(params, "limit", 200)
        store = self._store()
        try:
            todos = store.list_todos(status or None)
        finally:
            store.close()
        rows = [t.as_dict() for t in todos[:limit]]
        self._send(200, {"count": len(rows), "total": len(todos), "todos": rows})

    def _handle_stats(self) -> None:
        store = self._store()
        try:
            stats = store.stats()
            weekly = store.weekly_stats()
        finally:
            store.close()
        self._send(200, {"stats": stats, "weekly": weekly})

    def _handle_search(self, params: dict) -> None:
        keyword = (params.get("q") or [""])[0].strip()
        if not keyword:
            self._send(400, {"error": "缺少查询参数 q"})
            return
        limit = self._int_param(params, "limit", 20)
        store = self._store()
        try:
            rows = store.search(keyword, limit=limit)
        finally:
            store.close()
        self._send(200, {"count": len(rows), "messages": rows})

    def _handle_done(self, raw_id: str) -> None:
        try:
            todo_id = int(raw_id)
        except ValueError:
            self._send(400, {"error": "待办 id 必须是整数"})
            return

        body = self._read_json_body()
        if body is _BAD_BODY:
            self._send(400, {"error": "请求体必须是合法 JSON"})
            return
        status = (body or {}).get("status", "done")
        if status not in _ALLOWED_STATUS:
            self._send(400, {"error": f"status 只能是 {'/'.join(_ALLOWED_STATUS)}"})
            return

        store = self._store()
        try:
            ok = store.set_status(todo_id, status)
        finally:
            store.close()
        if not ok:
            self._send(404, {"error": f"没有编号为 {todo_id} 的待办"})
            return
        self._send(200, {"ok": True, "id": todo_id, "status": status})

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return _BAD_BODY
        if length <= 0:
            return {}
        if length > MAX_BODY:
            return _BAD_BODY
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _BAD_BODY
        return data if isinstance(data, dict) else _BAD_BODY


def _version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:              # noqa: BLE001
        return "unknown"


def make_handler(store_path: str, verbose: bool = False) -> type:
    """把库路径固化进 handler 类（每个请求各开一条连接，避免跨线程共享）。"""

    class Handler(_ApiHandler):
        pass

    Handler.store_path = store_path
    Handler.verbose = verbose
    return Handler


def create_server(store_path: str, host: str = DEFAULT_HOST,
                  port: int = DEFAULT_PORT, verbose: bool = False) -> ThreadingHTTPServer:
    """创建（但不启动）HTTP 服务；port=0 时由系统分配空闲端口。"""
    server = ThreadingHTTPServer((host, port), make_handler(store_path, verbose))
    server.daemon_threads = True
    server.verbose = verbose
    return server


def serve(store_path: str, host: str = DEFAULT_HOST,
          port: int = DEFAULT_PORT, verbose: bool = False) -> Tuple[str, int]:
    """阻塞运行，直到 Ctrl+C。返回实际监听地址。"""
    server = create_server(store_path, host, port, verbose)
    addr, real_port = server.server_address[0], server.server_address[1]
    try:
        print(f"TodoSeine API 已启动：http://{addr}:{real_port}")
        print("可用端点：GET /health, /todos, /stats, /search?q= ; POST /todos/{id}/done")
        print("按 Ctrl+C 停止")
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return addr, real_port
