"""SBOM 与许可证清单：从 pyproject.toml 的声明依赖生成。

输出两份文件：
  * ``sbom.json``    —— CycloneDX 1.4 风格的最小软件物料清单（组件 + 版本 + 作用域）；
  * ``licenses.md``  —— 依赖许可证表格（能从本机已安装包元数据读到就填真值）。

核心包（src/todoseine 运行所需）保持零第三方依赖，因此这里的组件全是
可选依赖（qt / api / db / snap / dev），不影响「开箱即用、零依赖」的定位。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from .. import __version__

_SPEC = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(.*)$")


def _split_requirement(text: str) -> Tuple[str, str]:
    """把 ``PySide6>=6.6`` 拆成 (名称, 版本约束)；没有约束时版本为空。"""
    match = _SPEC.match(str(text or ""))
    if not match:
        return str(text or "").strip(), ""
    return match.group(1), match.group(2).strip()


def _strip_comment(raw: str) -> str:
    """去掉行尾注释，但不动引号里的 #。"""
    quote = ""
    out = []
    for ch in raw:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out)


def _balanced(text: str) -> bool:
    depth = 0
    quote = ""
    for ch in text:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
    return depth <= 0


def _declared(path: str) -> Dict[Tuple[str, str], str]:
    """读取 pyproject 里 (section, key) → 原始值，支持跨行数组。"""
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    values: Dict[Tuple[str, str], str] = {}
    section = ""
    buf = ""
    pending_key = ""
    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if buf:
            buf = f"{buf} {line}".strip()
            if _balanced(buf):
                values[(section, pending_key)] = buf
                buf = pending_key = ""
            continue
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value.startswith("[") and not _balanced(value):
            buf, pending_key = value, key
            continue
        values[(section, key)] = value
    return values


def _list_items(value: str) -> List[str]:
    """``["a>=1", "b"]`` → ['a>=1', 'b']；不是数组时按单项处理。"""
    from ..config import _split_list

    text = (value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    if not text:
        return []
    items = []
    for part in _split_list(text):
        part = part.strip()
        if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'":
            part = part[1:-1]
        if part:
            items.append(part)
    return items


def _license_of(name: str) -> str:
    """优先读本机已安装包的元数据；没装就标「见 PyPI」。"""
    try:
        from importlib.metadata import metadata

        meta = metadata(name)
    except Exception:  # noqa: BLE001 - 没装 / 元数据缺失都走兜底
        return "未安装（见 PyPI 元数据）"
    lic = (meta.get("License") or "").strip()
    if lic and lic.upper() != "UNKNOWN":
        return lic.splitlines()[0][:60]
    for item in meta.get_all("Classifier") or []:
        if item.startswith("License ::"):
            return item.split("::")[-1].strip()
    return "未声明（见 PyPI 元数据）"


def collect_components(pyproject_path: str) -> Dict[str, List[Dict[str, str]]]:
    """返回 {'main': [...], 'optional': {extra: [...]}} 的扁平组件列表。"""
    values = _declared(pyproject_path)
    main: List[Dict[str, str]] = []
    for item in _list_items(values.get(("project", "dependencies"), "")):
        name, spec = _split_requirement(item)
        main.append({"name": name, "requirement": spec, "scope": "required"})
    optional: Dict[str, List[Dict[str, str]]] = {}
    for (section, extra), raw in sorted(values.items()):
        if section != "project.optional-dependencies":
            continue
        rows: List[Dict[str, str]] = []
        for item in _list_items(raw):
            name, spec = _split_requirement(item)
            rows.append({"name": name, "requirement": spec, "scope": f"optional:{extra}"})
        optional[extra] = rows
    return {"main": main, "optional": optional}


def build_sbom(pyproject_path: str) -> Dict[str, Any]:
    parts = collect_components(pyproject_path)
    components: List[Dict[str, Any]] = []
    seen = set()
    for row in parts["main"]:
        if row["name"] in seen:
            continue
        seen.add(row["name"])
        components.append({
            "type": "library",
            "name": row["name"],
            "version": row["requirement"] or "unspecified",
            "purl": f"pkg:pypi/{row['name'].lower()}",
            "scope": "required",
        })
    for rows in parts["optional"].values():
        for row in rows:
            if row["name"] in seen:
                continue
            seen.add(row["name"])
            components.append({
                "type": "library",
                "name": row["name"],
                "version": row["requirement"] or "unspecified",
                "purl": f"pkg:pypi/{row['name'].lower()}",
                "scope": "optional",
            })
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.4",
        "serialNumber": f"urn:uuid:todoseine-{datetime.now(timezone.utc):%Y%m%d%H%M%S}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "component": {
                "type": "application",
                "name": "todoseine",
                "version": __version__,
                "licenses": [{"license": {"id": "MIT"}}],
            },
            "properties": [
                {"name": "c2t:platform", "value": "Windows 11"},
                {"name": "c2t:core_dependencies", "value": "0"},
            ],
        },
        "components": components,
    }


def build_license_table(pyproject_path: str) -> str:
    parts = collect_components(pyproject_path)
    lines = [
        "# 第三方依赖许可证清单",
        "",
        f"生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"TodoSeine 版本：{__version__}",
        "",
        "核心包运行时不依赖任何第三方库；下表列出的都是可选组件，",
        "只有安装对应 extras 时才会进入环境。许可证取自本机已安装包元数据，",
        "未安装的标注为「见 PyPI 元数据」，发布前请以 PyPI 页面为准。",
        "",
        "| 组件 | 版本约束 | 作用域 | 许可证 |",
        "| --- | --- | --- | --- |",
    ]
    rows: List[Dict[str, str]] = []
    rows.extend(parts["main"])
    for rows_extra in parts["optional"].values():
        rows.extend(rows_extra)
    for row in rows:
        lines.append(
            f"| {row['name']} | {row['requirement'] or '未指定'} | "
            f"{row['scope']} | {_license_of(row['name'])} |"
        )
    lines.append("")
    lines.append("本清单由 `c2t sbom` 自动生成，不构成法律意见。")
    return "\n".join(lines) + "\n"


def write_all(out_dir: str, pyproject_path: str) -> Tuple[str, str]:
    """写入 sbom.json 与 licenses.md，返回两个路径。"""
    os.makedirs(out_dir, exist_ok=True)
    bom_path = os.path.join(out_dir, "sbom.json")
    license_path = os.path.join(out_dir, "licenses.md")
    with open(bom_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(build_sbom(pyproject_path), fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    with open(license_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(build_license_table(pyproject_path))
    return bom_path, license_path
