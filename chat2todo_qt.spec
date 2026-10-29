# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Chat2Todo Qt app (PySide6 6.12.0).

PySide6 6.12.0 的 DLL 直接放在 PySide6 根目录（没有 bin/ 子目录），
PyInstaller 官方 hook 可能漏收集——这里显式 glob 全部带上。
"""

from __future__ import annotations

import glob
import os
import sys

block_cipher = None

from PyInstaller.utils.hooks import collect_submodules

HERE = os.path.abspath(os.getcwd())
# 构建时 sys.executable 指向 .venv\Scripts\python.exe
VENV = os.path.dirname(os.path.dirname(sys.executable))
PYSIDE6_ROOT = os.path.join(VENV, "Lib", "site-packages", "PySide6")
SHIBOKEN6_ROOT = os.path.join(VENV, "Lib", "site-packages", "shiboken6")


# ---------- 收集 DLL ----------

# 只带 Widgets 应用真正用得到的 Qt DLL。
# PySide6 安装包里还塞了 Quick/Qml/3D/Designer/WebEngine 等 120MB+ 的模块，
# 静态分析虽然扫不到，但全量 glob 会把它们一并打进 exe —— 这里改成白名单。
KEEP_DLLS = {
    "Qt6Core", "Qt6Gui", "Qt6Widgets", "Qt6Network", "Qt6Svg",
    "Qt6PrintSupport", "Qt6OpenGL", "Qt6OpenGLWidgets", "Qt6DBus",
}

binaries = []

for dll in glob.glob(os.path.join(PYSIDE6_ROOT, "Qt6*.dll")):
    stem = os.path.splitext(os.path.basename(dll))[0]
    if stem in KEEP_DLLS:
        binaries.append((dll, "PySide6"))

# shiboken6 的 dll / pyd
for pattern in ("*.dll", "*.pyd"):
    for f in glob.glob(os.path.join(SHIBOKEN6_ROOT, pattern)):
        binaries.append((f, "shiboken6"))

# PySide6 插件：只要平台、样式、图片格式、图标引擎四类
KEEP_PLUGIN_DIRS = {"platforms", "styles", "imageformats", "iconengines"}
_plugins_root = os.path.join(PYSIDE6_ROOT, "plugins")
if os.path.isdir(_plugins_root):
    for root, dirs, files in os.walk(_plugins_root):
        rel = os.path.relpath(root, _plugins_root)
        top = rel.split(os.sep)[0]
        if top not in KEEP_PLUGIN_DIRS:
            continue
        for f in files:
            full = os.path.join(root, f)
            target = os.path.join("PySide6", "plugins", rel)
            binaries.append((full, target))

# PySide6 qt.conf + translation (最小集合)
_qt_conf = os.path.join(PYSIDE6_ROOT, "qt.conf")
if os.path.isfile(_qt_conf):
    binaries.append((_qt_conf, "PySide6"))


a = Analysis(
    ["tools/qt_launcher.py"],
    pathex=[HERE, os.path.join(HERE, "src")],
    binaries=binaries,
    datas=[],
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtNetwork",
        "PySide6.QtSvg",
        # Win11 OCR 主路径：winrt 子模块是运行期按需导入的，静态分析扫不到
        "PIL.ImageGrab",
        *collect_submodules("winrt"),
        # QQ 库直连：sources/db.py 在函数内部 import，静态分析扫不到；
        # 漏了它打包版会报 E4002「缺少 sqlcipher3 引擎」
        "sqlcipher3",
        "sqlcipher3.dbapi2",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["tools/runtime_hook_qt.py"],
    excludes=[
        "tkinter",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuick3D",
        "PySide6.QtPdf",
        "PySide6.QtMultimedia",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtDesigner",
        "PySide6.QtSql",
        "PySide6.QtTest",
        "PySide6.QtBluetooth",
        "PySide6.QtPositioning",
        "PySide6.QtSensors",
        "PySide6.QtSerialPort",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="chat2todo_qt",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,    # 发布版：无控制台；错误写 ~/.chat2todo/app.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(HERE, "assets", "chat2todo.ico"),
    version=os.path.join(HERE, "tools", "version_info.txt"),
)
