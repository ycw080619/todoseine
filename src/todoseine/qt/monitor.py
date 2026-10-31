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
from ..sources.uia import current_group_name, enum_qq_hwnds, fetch_messages

logger = logging.getLogger("todoseine.qt.monitor")


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
        self._last_db_import = 0.0

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
        self._ignored_names_cache = None
        self._wake_evt.set()

    # ---- 免打扰群名识别（OCR 通道用） ----

    def _ignored_group_names(self) -> set:
        """返回免打扰群对应的群名集合（用于 OCR 通道按群名过滤）。

        OCR 拿不到 chat_id，只能靠识别窗口标题栏的群名来判断是否免打扰。
        读 group_info.db 把 ignore_groups 里的群号翻译成群名，结果缓存
        10 分钟（群名很少变）。任何一步失败都返回空集合 → 降级为不过滤。
        """
        now = time.time()
        cached = getattr(self, "_ignored_names_cache", None)
        if cached and now - cached[0] < 600:
            return cached[1]
        try:
            from ..sources.db import _id_set, fetch_group_names
            conf = self._cfg.get("filter", {}) if isinstance(self._cfg, dict) else {}
            ignore = _id_set(conf.get("ignore_groups", []))
            if not ignore:
                self._ignored_names_cache = (now, set())
                return set()
            name_map = fetch_group_names(self._cfg)   # {群号或代码: 群名}
            names = {n for k, n in name_map.items() if k in ignore and n}
            self._ignored_names_cache = (now, names)
            return names
        except Exception as exc:  # noqa: BLE001 - 群名识别失败不影响主流程
            logger.debug("获取免打扰群名失败，降级为不过滤：%s", exc)
            self._ignored_names_cache = (now, set())
            return set()

    @staticmethod
    def _name_matches(ocr_name: str, ignored: set) -> bool:
        """OCR 识别的群名是否命中免打扰名单（模糊匹配，容错 OCR 误差）。"""
        ocr = (ocr_name or "").strip()
        if not ocr:
            return False
        for g in ignored:
            g = (g or "").strip()
            if not g:
                continue
            # 双向包含：OCR 可能多字少字，任一方向包含就算命中
            if ocr in g or g in ocr:
                return True
            # 去掉空白和常见 OCR 误差再比一次
            a = ocr.replace(" ", "").replace("　", "")
            b = g.replace(" ", "").replace("　", "")
            if a and b and (a in b or b in a):
                return True
        return False

    # ---- 主循环 ----

    def run(self) -> None:
        # 启动时先补抽一批「从未被抽取过」的历史消息。
        # 老版本未跑抽取、或 --days 窗口外的消息会永久遗漏，这里兜底。
        try:
            self._backfill_missing()
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动补抽失败：%s", exc)
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

    def _backfill_missing(self) -> None:
        """扫描 messages 表里从未被抽取过的消息，补跑一次抽取。"""
        if not self._db_path:
            return
        store = Store(self._db_path)
        try:
            pending = store.pending_reextract(limit=200)
            if not pending:
                return
            msgs = [
                Message(
                    timestamp=int(r["ts"]),
                    sender_id=str(r.get("sender_id") or ""),
                    sender_name=str(r.get("sender_name") or ""),
                    content=r["content"] or "",
                    type=int(r["type"]) if r.get("type") is not None else 0,
                    chat_id=str(r.get("chat_id") or ""),
                )
                for r in pending
            ]
            max_id = store.conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM todos").fetchone()[0]
            todos, decisions = extract_batch(
                msgs, self._cfg, session="qq",
                weights_map=store.load_feedback_weights())
            if todos:
                store.add_todos(todos)
                store.add_decisions(decisions)
                rows = store.conn.execute(
                    "SELECT * FROM todos WHERE id > ? ORDER BY id",
                    (int(max_id),)).fetchall()
                new_items = [_row_to_item(r) for r in rows]
                if new_items:
                    self.new_todos.emit(new_items)
                logger.info("启动补抽：新增 %d 条待办（扫描 %d 条未抽取消息）",
                            len(new_items), len(pending))
            else:
                # 即使没有新待办，也把判定记录落库，避免下次重复扫描
                store.add_decisions(decisions)
                logger.info("启动补抽：扫描 %d 条未抽取消息，无新待办", len(pending))
        finally:
            store.close()

    def _db_import_once(self) -> None:
        """从 QQ 本地数据库（nt_msg.db）导入最近 1 天的新消息并抽取待办。

        与 OCR 通道互补：OCR 只看当前屏幕，DB 通道能抓到所有群的消息。
        只取最近 1 天，避免每次都扫全库。走完整抽取管线（含 AI 审查）。
        """
        if not self._db_path:
            return
        from ..sources.db import load_messages
        from ..cli import _run_extraction
        msgs = load_messages(self._cfg, days=1)
        if not msgs:
            return
        store = Store(self._db_path)
        try:
            added = store.add_messages(msgs, session="qq")
            if not added:
                return
            # 只抽取「真正新增、且从未被抽取过」的消息：
            # load_messages 取最近 1 天全量，add_messages 靠 ON CONFLICT 跳过老的，
            # 但 msgs 里仍包含老消息。若对它们重跑抽取，AI 改写可能产出不同文本，
            # 绕过 add_todos 的文本去重，造成同一条通知反复入库。
            # 用 decisions 表的 content_hash 判定「是否已抽取过」，已抽取的直接跳过。
            hashes = {m.content_hash for m in msgs}
            if hashes:
                placeholders = ",".join("?" for _ in hashes)
                seen = {
                    r[0] for r in store.conn.execute(
                        f"SELECT DISTINCT content_hash FROM decisions "
                        f"WHERE content_hash IN ({placeholders})",
                        list(hashes),
                    ).fetchall()
                }
                if seen:
                    before = len(msgs)
                    msgs = [m for m in msgs if m.content_hash not in seen]
                    logger.debug("DB 导入去重：%d 条中 %d 条已抽取过，跳过",
                                 before, before - len(msgs))
            if not msgs:
                logger.info("DB 导入：新增 %d 条消息，但均已抽取过，无新待办", added)
                return
            max_id = store.conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM todos").fetchone()[0]
            todos, decisions, llm_records, _notes = _run_extraction(
                msgs, self._cfg, session="qq",
                weights_map=store.load_feedback_weights())
            store.add_llm_calls(llm_records)
            if todos:
                store.add_todos(todos)
                store.add_decisions(decisions)
                rows = store.conn.execute(
                    "SELECT * FROM todos WHERE id > ? ORDER BY id",
                    (int(max_id),)).fetchall()
                new_items = [_row_to_item(r) for r in rows]
                if new_items:
                    self.new_todos.emit(new_items)
                logger.info("DB 导入：新增 %d 条消息，%d 条待办", added, len(new_items))
            else:
                store.add_decisions(decisions)
                logger.info("DB 导入：新增 %d 条消息，无新待办", added)
        finally:
            store.close()

    def _run_once(self) -> None:
        # 定时从 QQ 本地数据库（nt_msg.db）导入新消息。
        # OCR 通道只能看到当前屏幕上的聊天，DB 通道能抓到所有群的历史消息，
        # 两者互补。默认每 5 分钟跑一次（可被 [uia].db_import_interval 覆盖）。
        now = time.time()
        db_interval = int((self._cfg.get("uia") or {}).get("db_import_interval") or 300)
        if now - self._last_db_import >= db_interval:
            self._last_db_import = now
            try:
                self._db_import_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning("定时 DB 导入失败：%s", exc)

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

        # 免打扰群过滤：OCR 拿不到 chat_id，先识别当前群名，若是免打扰群
        # （且非 @全体成员 通知）就整轮跳过，避免闲聊入库和归档。
        ignored_names = self._ignored_group_names()
        if ignored_names:
            try:
                gname = current_group_name(hwnd=int(uia.get("hwnd") or 0))
            except Exception as exc:  # noqa: BLE001
                gname = ""
                logger.debug("群名识别失败：%s", exc)
            if gname and self._name_matches(gname, ignored_names):
                logger.info("当前群「%s」在免打扰名单，跳过本轮 OCR 抓取", gname)
                self.ticked.emit({"messages": 0, "todos": 0, "low_conf": 0,
                                  "error": "", "at": time.time(),
                                  "elapsed": time.time() - started})
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
                # 跨通道去重：DB 通道已入库的消息（有真实 chat_id），
                # OCR 不再重复存——否则同一条通知会被两个通道各存一份，
                # 还可能各自抽成待办。
                hashes = {m.content_hash for m in msgs}
                if hashes:
                    placeholders = ",".join("?" for _ in hashes)
                    seen = {
                        r[0] for r in store.conn.execute(
                            f"SELECT content_hash FROM messages "
                            f"WHERE chat_id != '' AND content_hash IN ({placeholders})",
                            list(hashes),
                        ).fetchall()
                    }
                    if seen:
                        before = len(msgs)
                        msgs = [m for m in msgs if m.content_hash not in seen]
                        logger.debug("跨通道去重：OCR %d 条中 %d 条已在 DB，跳过",
                                     before, before - len(msgs))
                # 已抽取过的消息不再重跑 AI：避免 rewrite 产出不同文本绕过文本去重。
                # 用 decisions.content_hash 判定（覆盖 OCR 自身和 DB 两个通道的历史抽取）。
                hashes = {m.content_hash for m in msgs}
                if hashes:
                    placeholders = ",".join("?" for _ in hashes)
                    seen_dec = {
                        r[0] for r in store.conn.execute(
                            f"SELECT DISTINCT content_hash FROM decisions "
                            f"WHERE content_hash IN ({placeholders})",
                            list(hashes),
                        ).fetchall()
                    }
                    if seen_dec:
                        before = len(msgs)
                        msgs = [m for m in msgs if m.content_hash not in seen_dec]
                        logger.debug("OCR 去重：%d 条中 %d 条已抽取过，跳过",
                                     before, before - len(msgs))
                if not msgs:
                    self._fails = 0
                    self.ticked.emit({"messages": 0, "todos": 0, "low_conf": low_n,
                                      "error": "", "at": time.time(),
                                      "elapsed": time.time() - started})
                    return
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
