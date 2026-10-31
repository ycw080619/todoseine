"""插件宿主：清单扫描 / 子进程隔离 / 权限白名单 / 崩溃计数 / 安全模式 / 回滚。

文档口径（逐条落地）：
  * 插件跑在独立子进程，事件由宿主分发，崩溃只终止自己；
  * 插件只能使用 manifest 里声明的权限（白名单），未声明的事件直接拒绝；
  * 7 天窗口内同一个插件累计 2 次崩溃 → 进入安全模式，引导用户回滚；
  * 安装 / 启用插件前自动备份当前插件与配置状态；回滚只恢复插件与配置，
    不碰数据库，也不中断主流程。

插件协议（v1，最小可用）：
  目录结构 ``<plugins>/<id>/manifest.json + entry.py``；
  宿主以 ``python -I entry.py`` 启动子进程，把事件 JSON 放进环境变量
  ``C2T_EVENT``，插件把结果 JSON 打到 stdout 最后一行；退出码非 0 或超时算崩溃。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..plugin_manifest import Manifest, ManifestError, validate_manifest

log = logging.getLogger("todoseine.plugins")

STATE_NAME = ".state.json"
CRASH_WINDOW = timedelta(days=7)
CRASH_LIMIT = 2
KEEP_SNAPSHOTS = 5
DEFAULT_TIMEOUT = 10.0

# 事件 → 需要的权限
EVENT_PERMISSION = {
    "message.new": "messages.read",
    "todo.created": "todos.read",
    "todo.updated": "todos.read",
    "todo.completed": "todos.read",
    "notify": "notify.write",
}


class PluginError(Exception):
    """插件调用被拒绝或执行失败。"""


@dataclass
class Plugin:
    path: str
    manifest: Manifest
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def permissions(self) -> List[str]:
        return [str(x) for x in (self.raw.get("permissions") or [])]

    def allows(self, event: str) -> bool:
        need = EVENT_PERMISSION.get(event)
        return bool(need) and need in self.permissions


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class PluginManager:
    """进程内单例式管理器；状态落在 <plugins>/.state.json。"""

    def __init__(self, folder: Optional[str] = None, python: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT):
        base = folder or os.path.join(os.path.expanduser("~"), ".chat2todo")
        self.folder = os.path.join(base, "plugins") if not folder else folder
        self.state_path = os.path.join(self.folder, STATE_NAME)
        self.python = python or sys.executable
        self.timeout = timeout
        self._state: Optional[Dict[str, Any]] = None

    # ---------- 状态 ----------

    def _load(self) -> Dict[str, Any]:
        if self._state is not None:
            return self._state
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._state = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._state = {}
        self._state.setdefault("enabled", [])
        self._state.setdefault("crashes", {})
        self._state.setdefault("safe_mode", False)
        self._state.setdefault("snapshots", [])
        self._state.setdefault("disabled_before_safe_mode", [])
        return self._state

    def _save(self) -> None:
        state = self._load()
        try:
            os.makedirs(self.folder, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.warning("写插件状态失败：%s", exc)

    # ---------- 扫描 ----------

    def scan(self) -> List[Plugin]:
        """列出 plugins 目录下所有 manifest 合法的插件；坏清单跳过并记日志。"""
        found: List[Plugin] = []
        try:
            entries = sorted(os.listdir(self.folder))
        except OSError:
            return found
        for name in entries:
            path = os.path.join(self.folder, name)
            manifest_file = os.path.join(path, "manifest.json")
            if not os.path.isdir(path) or not os.path.isfile(manifest_file):
                continue
            try:
                with open(manifest_file, "r", encoding="utf-8-sig") as fh:
                    raw = json.load(fh)
                manifest = validate_manifest(raw)
            except (OSError, ValueError, ManifestError, KeyError) as exc:
                log.warning("插件 %s 的 manifest 无效：%s", name, exc)
                continue
            found.append(Plugin(path=path, manifest=manifest, raw=raw))
        return found

    def get(self, plugin_id: str) -> Optional[Plugin]:
        return next((p for p in self.scan() if p.id == plugin_id), None)

    def is_enabled(self, plugin_id: str) -> bool:
        return plugin_id in self._load()["enabled"]

    def enabled_plugins(self) -> List[Plugin]:
        enabled = set(self._load()["enabled"])
        return [p for p in self.scan() if p.id in enabled]

    # ---------- 安全模式 ----------

    @property
    def safe_mode(self) -> bool:
        return bool(self._load()["safe_mode"])

    def enter_safe_mode(self, reason: str = "") -> None:
        state = self._load()
        if state["safe_mode"]:
            return
        state["safe_mode"] = True
        state["disabled_before_safe_mode"] = list(state["enabled"])
        state["safe_mode_reason"] = reason
        state["enabled"] = []
        self._save()
        log.warning("插件进入安全模式：%s", reason or "崩溃次数超限")

    def exit_safe_mode(self, restore: bool = False) -> List[str]:
        """退出安全模式；restore=True 时恢复进入前的启用列表。"""
        state = self._load()
        state["safe_mode"] = False
        state.pop("safe_mode_reason", None)
        restored = list(state.get("disabled_before_safe_mode") or []) if restore else []
        if restore:
            state["enabled"] = restored
        state["disabled_before_safe_mode"] = []
        self._save()
        return restored

    # ---------- 崩溃计数 ----------

    def record_crash(self, plugin_id: str, message: str = "") -> Dict[str, Any]:
        """记一次崩溃；7 天内累计 2 次自动进入安全模式。"""
        state = self._load()
        now = datetime.now()
        history = [t for t in (state["crashes"].get(plugin_id) or [])
                   if _within(t, now)]
        history.append(_iso(now))
        state["crashes"][plugin_id] = history
        result: Dict[str, Any] = {
            "plugin": plugin_id,
            "crashes_in_window": len(history),
            "safe_mode": state["safe_mode"],
            "message": message,
        }
        if len(history) >= CRASH_LIMIT and not state["safe_mode"]:
            self.enter_safe_mode(f"{plugin_id} 在 7 天内崩溃 {len(history)} 次")
            result["safe_mode"] = True
        self._save()
        log.warning("插件 %s 崩溃（7 天内 %d 次）：%s", plugin_id, len(history), message)
        return result

    # ---------- 启用 / 停用 ----------

    def set_enabled(self, plugin_id: str, enabled: bool = True,
                    cfg_file: str = "") -> Dict[str, Any]:
        """启用前自动备份；停用只改状态。"""
        plugin = self.get(plugin_id)
        if plugin is None:
            raise PluginError(f"找不到插件 {plugin_id}")
        state = self._load()
        if enabled and self.safe_mode:
            raise PluginError("安全模式下不能启用插件，请先退出安全模式")
        snapshot = ""
        if enabled and plugin_id not in state["enabled"]:
            snapshot = self.backup(cfg_file)
        if enabled and plugin_id not in state["enabled"]:
            state["enabled"].append(plugin_id)
        if not enabled and plugin_id in state["enabled"]:
            state["enabled"].remove(plugin_id)
        self._save()
        return {"plugin": plugin_id, "enabled": enabled, "snapshot": snapshot}

    # ---------- 备份 / 回滚 ----------

    def backup(self, cfg_file: str = "") -> str:
        """把 plugins 目录与配置文件打成一个 zip，供回滚使用。"""
        os.makedirs(self.folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = os.path.join(self.folder, f"backup-{stamp}.zip")
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _dirs, files in os.walk(self.folder):
                    for name in files:
                        full = os.path.join(root, name)
                        if os.path.abspath(full) == os.path.abspath(target):
                            continue
                        rel = os.path.relpath(full, self.folder)
                        zf.write(full, os.path.join("plugins", rel))
                if cfg_file and os.path.isfile(cfg_file):
                    zf.write(cfg_file, os.path.join("config", os.path.basename(cfg_file)))
        except OSError as exc:
            log.warning("插件备份失败：%s", exc)
            return ""
        state = self._load()
        state["snapshots"].append(target)
        while len(state["snapshots"]) > KEEP_SNAPSHOTS:
            old = state["snapshots"].pop(0)
            try:
                os.remove(old)
            except OSError:
                pass
        self._save()
        return target

    def rollback(self, cfg_dir: str = "") -> Optional[str]:
        """恢复到最近一次备份；返回 zip 路径，没有备份返回 None。"""
        import shutil
        import tempfile

        state = self._load()
        candidates = [p for p in state["snapshots"] if os.path.isfile(p)]
        if not candidates:
            return None
        latest = candidates[-1]
        staging = tempfile.mkdtemp(prefix="c2t-plugins-")
        try:
            with zipfile.ZipFile(latest) as zf:
                for info in zf.infolist():
                    name = info.filename.replace("\\", "/")
                    if ".." in name.split("/"):
                        continue
                    zf.extract(info, staging)
            src_plugins = os.path.join(staging, "plugins")
            if os.path.isdir(src_plugins):
                shutil.copytree(src_plugins, self.folder, dirs_exist_ok=True)
            src_config = os.path.join(staging, "config")
            if cfg_dir and os.path.isdir(src_config):
                shutil.copytree(src_config, cfg_dir, dirs_exist_ok=True)
            state["safe_mode"] = False
            self._save()
            log.info("插件已回滚：%s", latest)
            return latest
        except (OSError, zipfile.BadZipFile) as exc:
            log.warning("插件回滚失败：%s", exc)
            return None
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # ---------- 调用 ----------

    def invoke(self, plugin_id: str, event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """在子进程里调用插件；权限不足 / 安全模式 / 崩溃都在这里拦下。"""
        if self.safe_mode:
            raise PluginError("安全模式：所有第三方插件已禁用")
        plugin = self.get(plugin_id)
        if plugin is None:
            raise PluginError(f"找不到插件 {plugin_id}")
        if not self.is_enabled(plugin_id):
            raise PluginError(f"插件 {plugin_id} 未启用")
        if not plugin.allows(event):
            raise PluginError(f"插件 {plugin_id} 未声明权限 {EVENT_PERMISSION.get(event, event)}")
        entry = os.path.join(plugin.path, plugin.manifest.entry or "entry.py")
        if not os.path.isfile(entry):
            return self._fail(plugin_id, f"入口文件不存在：{entry}")

        env = dict(os.environ)
        env["C2T_EVENT"] = json.dumps({"event": event, "payload": payload},
                                      ensure_ascii=False)
        env["C2T_PLUGIN_ID"] = plugin_id
        env.pop("PYTHONPATH", None)
        try:
            proc = subprocess.run(
                [self.python, "-I", entry],
                cwd=plugin.path, env=env, capture_output=True,
                timeout=self.timeout, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return self._fail(plugin_id, f"执行超时（>{self.timeout}s）")
        except OSError as exc:
            return self._fail(plugin_id, f"启动失败：{exc}")

        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
            return self._fail(plugin_id, f"退出码 {proc.returncode}：{err}")
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        parsed: Any = None
        for line in reversed(out.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                break
            except ValueError:
                continue
        return {"ok": True, "plugin": plugin_id, "result": parsed}

    def _fail(self, plugin_id: str, message: str) -> Dict[str, Any]:
        info = self.record_crash(plugin_id, message)
        return {"ok": False, "plugin": plugin_id, "error": message,
                "safe_mode": info["safe_mode"]}


def _within(iso_text: str, now: datetime) -> bool:
    try:
        return now - datetime.fromisoformat(iso_text) <= CRASH_WINDOW
    except ValueError:
        return False


def crash_count(state: Dict[str, Any], plugin_id: str,
                now: Optional[datetime] = None) -> int:
    """测试 / 设置页展示用：7 天内崩溃次数。"""
    now = now or datetime.now()
    return len([t for t in (state.get("crashes", {}).get(plugin_id) or [])
                if _within(t, now)])


def list_plugins(folder: Optional[str] = None) -> List[Tuple[str, str, bool]]:
    """给 CLI 用的轻量列表：(id, name, 是否启用)。"""
    mgr = PluginManager(folder)
    return [(p.id, p.manifest.name, mgr.is_enabled(p.id)) for p in mgr.scan()]
