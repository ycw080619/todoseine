"""常驻监听管线：定时截图 OCR → 抽取待办 → 跨群合并 → 入库 → 通知看板。

为什么放在 QThread 里跑整条管线：
  Win11 OCR 一次要 0.5~2 秒，如果放在主线程，界面会每秒卡一下。
  线程内自己开 Store（SQLite 连接不能跨线程复用），跑完把结果通过
  信号丢回主线程；主线程只做刷新和弹通知。

对外契约：
  Monitor.start() / stop() / pause() / resume() / sync_now() / reconfigure(cfg)
  Monitor.todos_added(list[TodoItem])   本轮新入库的待办
  Monitor.ticked(dict)                  每轮摘要 {messages, todos, error, at}
  Monitor.status_changed(str)           给人看的一句话状态
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, QThread, Signal

from .. import config as c2t_config
from ..core import Store
from ..core.archive import Archiver as Archive
from ..core.events import merge_events
from ..core.extract import extract_todos
from ..models import Message, TodoItem
from ..sources.uia import enum_qq_hwnds, fetch_messages

logger = logging.getLogger("chat2todo.qt.monitor")


# ---------------------------------------------------------------------------
# 纯函数：一批消息 → 待办（方便单测，不碰 Qt / 数据库）
# ---------------------------------------------------------------------------

def extract_batch(messages: List[Message], cfg: dict,
                  session: str = "QQ实时",
                  weights_map: Optional[dict] = None) -> tuple:
    """把一批消息跑成待办。返回 (todos, decisions)。

    实时通道不用向量清洗（Ollama 不在时会拖慢每一轮），
    去重交给 OCR 的三元组去重和 todos 表的唯一键。
    """
    decisions: list = []
    todos = extract_todos(
        messages,
        session=session,
        weights_map=weights_map,
        todo_threshold=int(cfg.get("todo_threshold") or 4),
        weak_threshold=int(cfg.get("pending_threshold") or 3),
        decisions=decisions,
    )
    todos, _note = merge_events(todos, messages)
    return todos, decisions


def _row_to_item(row) -> TodoItem:
    keys = row.keys()
    return TodoItem(
        todo_id=row["id"],
        text=row["text"],
        session=row["session"],
        sender=row["sender"],
        message_ts=row["ts"],
        reason=row["reason"],
        confidence=row["confidence"],
        status=row["status"],
        done_at=(row["done_at"] if "done_at" in keys else "") or "",
    )


# ---------------------------------------------------------------------------
# 工作线程
# ---------------------------------------------------------------------------

class _Worker(QThread):
    ticked = Signal(object)      # 摘要 dict
    new_todos = Signal(object)   # list[TodoItem]
    failed = Signal(str)

    def __init__(self, db_path: str, cfg: dict, interval: int,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self._db_path = db_path
        self._cfg = dict(cfg)
        self._interval = max(1, int(interval))
        self._stop_evt = threading.Event()
        self._pause_evt = threading.Event()
        self._wake_evt = threading.Event()
        self._fails = 0
        self._last_no_qq = 0.0

    # ---- 外部命令（都只置事件，线程安全） ----

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake_evt.set()

    def pause(self) -> None:
        self._pause_evt.set()
        self._wake_evt.set()

    def resume(self) -> None:
        self._pause_evt.clear()
        self._wake_evt.set()

    def sync_now(self) -> None:
        self._wake_evt.set()

    def set_interval(self, seconds: int) -> None:
        self._interval = max(1, int(seconds))

    def update(self, cfg: dict, db_path: str, interval: int) -> None:
        """热更新配置（下轮抓取立即生效）。"""
        self._cfg = dict(cfg)
        self._db_path = db_path
        self.set_interval(interval)
        self._wake_evt.set()

    # ---- 主循环 ----

    def run(self) -> None:
        while not self._stop_evt.is_set():
            if self._pause_evt.is_set():
                self._wake_evt.wait(1.0)
                self._wake_evt.clear()
                continue
            self._run_once()
            wait = self._interval
            if self._fails:
                # 连续失败时退避，最多 60 秒；成功一次立刻回到正常间隔
                wait = min(60, self._interval * (2 ** min(self._fails, 4)))
            self._wake_evt.wait(wait)
            self._wake_evt.clear()

    def _run_once(self) -> None:
        uia = self._cfg.get("uia") or {}
        session = str(uia.get("session") or "QQ实时")
        started = time.time()
        if not enum_qq_hwnds():
            # 没找到 QQ 窗口时每 5 分钟提示一次，避免每轮都弹
            now = time.time()
            if now - self._last_no_qq > 300:
                self._last_no_qq = now
                self.failed.emit("未找到 QQ 窗口，请先打开 QQ 并登录后重试")
            self.ticked.emit({"messages": 0, "todos": 0, "low_conf": 0,
                              "error": "", "at": now, "elapsed": now - started})
            return
        try:
            raw = fetch_messages(
                hwnd=int(uia.get("hwnd") or 0),
                min_confidence=float(uia.get("ocr_min_confidence") or 0.5),
                keep_low_confidence=bool(uia.get("keep_low_confidence", False)),
            )
        except Exception as exc:  # noqa: BLE001 - 采集失败不能弄死线程
            self._fails += 1
            logger.debug("抓取失败：%s", exc)
            self.failed.emit(f"抓取失败：{exc}")
            self.ticked.emit({"messages": 0, "todos": 0, "error": str(exc),
                              "at": time.time(), "elapsed": time.time() - started})
            return

        keep_low = bool(uia.get("keep_low_confidence", False))
        low_n = sum(1 for m in raw if m.low_conf)
        msgs = raw if keep_low else [m for m in raw if not m.low_conf]

        if not msgs:
            self._fails = 0
            self.ticked.emit({"messages": 0, "todos": 0, "low_conf": low_n,
                              "error": "", "at": time.time(),
                              "elapsed": time.time() - started})
            return

        if not self._db_path:
            self._fails = 1
            self.failed.emit("尚未配置数据库路径")
            return

        new_items: List[TodoItem] = []
        added = 0
        try:
            store = Store(self._db_path)
            try:
                added = store.add_messages(msgs, session=session)
                # 消息 Markdown 归档：群名/年/月/日.md + manifest.json 去重
                Archive(self._cfg, self._db_path).write(msgs, session=session)
                max_id = store.conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM todos").fetchone()[0]
                todos, decisions = extract_batch(
                    msgs, self._cfg, session, store.load_feedback_weights())
                if todos:
                    store.add_todos(todos)
                    store.add_decisions(decisions)
                    rows = store.conn.execute(
                        "SELECT * FROM todos WHERE id > ? ORDER BY id",
                        (int(max_id),)).fetchall()
                    new_items = [_row_to_item(r) for r in rows]
            finally:
                store.close()
            self._fails = 0
        except Exception as exc:  # noqa: BLE001 - 写库失败只影响本轮
            self._fails += 1
            logger.warning("监听入库失败：%s", exc)
            self.failed.emit(f"入库失败：{exc}")
            self.ticked.emit({"messages": 0, "todos": 0, "error": str(exc),
                              "at": time.time(), "elapsed": time.time() - started})
            return

        if new_items:
            self.new_todos.emit(new_items)
        self.ticked.emit({"messages": added, "todos": len(new_items), "low_conf": low_n,
                          "error": "", "at": time.time(), "elapsed": time.time() - started})


# ---------------------------------------------------------------------------
# 对外门面
# ---------------------------------------------------------------------------

class Monitor(QObject):
    """监听开关与状态的门面，给托盘 / 看板 / 设置页共用。"""

    status_changed = Signal(str)
    todos_added = Signal(list)
    ticked = Signal(dict)
    failed = Signal(str)

    def __init__(self, db_path: str = "", cfg: Optional[dict] = None,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self._cfg = cfg or c2t_config.load()
        self._db_path = db_path or str(self._cfg.get("db_path") or "")
        uia = self._cfg.get("uia") or {}
        self._interval = max(1, int(uia.get("interval") or 2))
        self._worker: Optional[_Worker] = None
        self._paused = False
        self._stopping = False
        self._last: Dict = {}
        self._status = "未启动"

    # ---------- 生命周期 ----------

    def is_paused(self) -> bool:
        return self._paused

    def status(self) -> str:
        return self._status

    def running(self) -> bool:
        """采集线程是否仍在跑（Hermes 巡检据此判断要不要自动重启）。"""
        return bool(self._worker is not None and self._worker.isRunning())

    def stopping(self) -> bool:
        """是否处于「已请求停止、线程尚未退出」的中间态。"""
        return bool(self._stopping)

    def start(self) -> None:
        if self._worker is not None:
            if self._worker.isRunning():
                return
            # 上次 stop 超时、线程后来自己收尾完的，先释放旧引用
            self._worker.deleteLater()
            self._worker = None
        self._stopping = False
        self._worker = _Worker(self._db_path, self._cfg, self._interval, self)
        self._worker.ticked.connect(self._on_ticked)
        self._worker.new_todos.connect(self.todos_added.emit)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()
        self._set_status("监听中")

    def stop(self) -> None:
        w = self._worker
        if w is None:
            return
        w.stop()
        if w.wait(1500):
            self._worker = None
            w.deleteLater()
            self._set_status("已停止")
            return
        # OCR 偶发要两三秒，不在 GUI 线程干等；线程退出后自己收尾。
        # 这里不能把引用置空，否则线程还在跑就被析构，Qt 会直接 abort。
        self._stopping = True
        self._set_status("正在停止…")
        w.finished.connect(lambda: self._on_worker_finished(w))

    def _on_worker_finished(self, w: "_Worker") -> None:
        if self._worker is w:
            self._worker = None
            self._stopping = False
            self._set_status("已停止")
        w.deleteLater()

    def pause(self) -> None:
        self._paused = True
        if self._worker:
            self._worker.pause()
        self._set_status("已暂停")

    def resume(self) -> None:
        self._paused = False
        if self._worker is not None:
            self._worker.resume()
            self._set_status("监听中")

    def toggle(self) -> bool:
        """暂停 ↔ 恢复，返回切换后是否仍在监听。"""
        if self._paused:
            self.resume()
        else:
            self.pause()
        return not self._paused

    def sync_now(self) -> None:
        if self._paused:
            self.resume()
        if self._worker is not None:
            self._worker.sync_now()
        self._set_status("正在同步…")

    # ---------- 配置 ----------

    def reconfigure(self, cfg: Optional[dict] = None) -> None:
        """设置页保存后调用：路径 / 间隔 / 窗口句柄都可能变了，重建线程。"""
        cfg = cfg or c2t_config.load()
        self._cfg = cfg
        self._db_path = str(cfg.get("db_path") or self._db_path)
        uia = cfg.get("uia") or {}
        self._interval = max(1, int(uia.get("interval") or 2))
        if self._worker is not None:
            self._worker.update(cfg, self._db_path, self._interval)

    # ---------- 内部 ----------

    def _set_status(self, text: str) -> None:
        self._status = text
        self.status_changed.emit(text)

    def _on_ticked(self, info: dict) -> None:
        if self._stopping:
            return          # 正在停：旧线程最后一轮的结果不能把状态改回「监听中」
        self._last = info
        self.ticked.emit(info)
        if info.get("error"):
            self._set_status("异常 · 稍后重试")
            return
        stamp = time.strftime("%H:%M:%S", time.localtime(info.get("at") or time.time()))
        if info.get("low_conf"):
            self._set_status(
                f"监听中 · {stamp} {info['low_conf']} 条 OCR 置信度偏低，已跳过入库")
        elif info.get("todos"):
            self._set_status(f"监听中 · {stamp} 新增 {info['todos']} 条")
        elif info.get("messages"):
            self._set_status(f"监听中 · {stamp} 读到 {info['messages']} 条消息")
        else:
            self._set_status(f"监听中 · {stamp} 无新消息")

    def _on_failed(self, text: str) -> None:
        self._set_status(f"异常：{text}")
        self.failed.emit(text)
