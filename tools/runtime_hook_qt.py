# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller runtime hook：启动时把 PyInstaller 解压目录加入 DLL 搜索路径。

PySide6 6.12.0 的 DLL 直接放在 PySide6 根目录，onefile 模式解压到 _MEIPASS
后，QtCore.pyd 需要在同目录找到 Qt6Core.dll。
"""

import os
import sys

# PyInstaller onefile 解压目录
base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

# 把 PySide6 目录、shiboken6 目录、以及根目录都加进 DLL 搜索路径
for sub in ("PySide6", "shiboken6", ""):
    path = os.path.join(base, sub) if sub else base
    if os.path.isdir(path):
        os.add_dll_directory(path) if hasattr(os, "add_dll_directory") else None
        os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
