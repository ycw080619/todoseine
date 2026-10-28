"""静态回归：源码里不允许出现「引用了但从未定义」的名字。

对应缺陷：dashboard 拆包时 `popup()` 引用了只存在于旧文件的 `menu_ref`，
右键任意条目必抛 NameError，而数据层单测完全覆盖不到这种 UI 闭包残留。
这里用标准库 `symtable` 做一次轻量 pyflakes（相当于 ruff 的 F821），
不依赖 GUI、也不依赖第三方包，可在无显示环境与 CI 里跑。
"""

from __future__ import annotations

import builtins
import symtable
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "chat2todo"

# 解释器在模块里隐式注入的全局名，不必显式定义
_IMPLICIT = {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__annotations__", "__cached__",
}


def _module_level_names(table: symtable.SymbolTable) -> set:
    """模块作用域里被绑定过的名字（赋值 / import / 形参）。"""
    return {s.get_name() for s in table.get_symbols()
            if s.is_assigned() or s.is_imported() or s.is_parameter()}


def _collect(table: symtable.SymbolTable, known: set, rel: str,
             findings: list) -> None:
    """递归检查：只被引用（is_global 且未赋值）、且不是模块名/内建名的，就是未定义名。"""
    for sym in table.get_symbols():
        name = sym.get_name()
        if (sym.is_global() and not sym.is_assigned()
                and name not in known and name not in _IMPLICIT
                and not hasattr(builtins, name)):
            findings.append(f"{rel}:{table.get_name()}: {name}")
    for child in table.get_children():
        _collect(child, known, rel, findings)


def scan_source(src: str, rel: str) -> list:
    """返回 [(位置, 名字)] 形式的未定义名列表。"""
    if "import *" in src:              # 星号导入无法静态判定，跳过
        return []
    top = symtable.symtable(src, rel, "exec")
    findings: list = []
    _collect(top, _module_level_names(top), rel, findings)
    return findings


class TestNoUndefinedNames(unittest.TestCase):
    def test_source_has_no_undefined_names(self) -> None:
        files = sorted(SRC.rglob("*.py"))
        self.assertTrue(files, f"没有找到源码：{SRC}")
        findings: list = []
        for py in files:
            rel = str(py.relative_to(SRC.parent.parent))
            findings += scan_source(py.read_text(encoding="utf-8"), rel)
        self.assertEqual([], findings,
                         "存在未定义名字：\n" + "\n".join(findings))
