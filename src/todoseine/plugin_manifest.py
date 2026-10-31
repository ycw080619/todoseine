"""插件注册协议（v1）：manifest JSON + 轻量校验器。

TodoSeine 的插件不进 pip，不走 discovery，靠一个明确的 manifest 文件
让控制台 / 未来的插件管理器「看见」它、「知道」它能干什么、「确认」
它有哪些系统权限。

manifest 格式（JSON, schema: chat2todo-plugin-v1）：
{
  "schema":         "chat2todo-plugin-v1",      // 必填
  "id":             "snap_todo",                  // 唯一 id, 小写 snake_case
  "name":           "截图待办",                   // 给用户看的中文名
  "version":        "0.1.0",                      // semver
  "author":         "TodoSeine contributors",
  "description":     "Win+Shift+S 框选 → OCR → 入库",
  "permission_level": "uia",                      // file / db / uia（适配器基类已预留）
  "entry":          "snap.py",                    // 相对 manifest 的入口脚本
  "capabilities":   ["ocr", "hotkey", "ai_refine"],   // 给控制台「首次使用引导」提示用
  "dependencies":   ["todoseine>=0.4.0"],        // 核心版本下限
  "triggers": {                                  // 可选：自动启动 / 计划任务配置
    "autostart": false,
    "hotkey":     "ctrl+shift+s"
  }
}

设计约束：
- manifest 本身是 static 文件，插件管理器不写它，只读它
- 这里只给一个 validator；真正的插件 loader 是以后的事
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

SCHEMA = "chat2todo-plugin-v1"
_PERMLV = ("file", "db", "uia")          # SourceAdapter.permission_level 三档
_SCHEMA_VERSION_FALLBACK = "chat2todo-plugin-v1"


@dataclass
class Manifest:
    """解析过的插件 manifest（只读）。"""
    schema: str
    id: str
    name: str
    version: str
    description: str
    author: str = ""
    permission_level: str = "file"
    entry: str = ""
    capabilities: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    triggers: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        return cls(
            schema=str(data.get("schema", "")),
            id=str(data["id"]),
            name=str(data["name"]),
            version=str(data["version"]),
            description=str(data.get("description", "")),
            author=str(data.get("author", "")),
            permission_level=str(data.get("permission_level", "file")),
            entry=str(data.get("entry", "")),
            capabilities=list(data.get("capabilities", [])),
            dependencies=list(data.get("dependencies", [])),
            triggers=dict(data.get("triggers", {})),
        )


class ManifestError(Exception):
    """manifest 校验失败的错误，带字段路径方便排障。"""
    def __init__(self, field: str, msg: str):
        self.field = field
        self.msg = msg
        super().__init__(f"manifest.{field}: {msg}")


def validate_manifest(path_or_dict) -> Optional[Manifest]:
    """校验一个 manifest 文件或 dict。成功返回 Manifest；失败抛 ManifestError。

    刻意保持严格（schema 不匹配直接拒、必填项缺失直接拒），但又不做
    「entry 文件必须存在」这类文件系统检查——因为 manifest 可能跨机器分发。
    """
    if isinstance(path_or_dict, (str, Path)):
        data = json.loads(Path(path_or_dict).read_text(encoding="utf-8"))
    elif isinstance(path_or_dict, dict):
        data = path_or_dict
    else:
        raise ManifestError("root", "必须是 dict 或 JSON 文件路径")

    # schema 宽松点——允许插件写 chat2todo-plugin-v1 或直接不写
    schema = data.get("schema", "")
    if schema and schema != SCHEMA:
        raise ManifestError("schema", f"不识别的 schema: {schema}（期望 {SCHEMA}）")

    # 必填
    for k in ("id", "name", "version"):
        if not data.get(k):
            raise ManifestError(k, "必填字段缺失或为空")

    pid = data["id"]
    if not pid.isidentifier() or pid != pid.lower():
        raise ManifestError("id", f"必须是小写 snake_case 标识符，got {pid!r}")

    perm = data.get("permission_level", "file")
    if perm not in _PERMLV:
        raise ManifestError("permission_level",
                            f"必须是 {'/'.join(_PERMLV)}，got {perm!r}")

    return Manifest.from_dict(data)
