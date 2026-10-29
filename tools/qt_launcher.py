"""Qt 版打包入口。

PyInstaller 会把入口脚本当 __main__ 执行，包内相对导入（from .. import config）
在这种上下文里会失败；所以真正的入口是这个小脚本，用绝对导入拉起应用。
开发时仍用 `python -m chat2todo.qt.main`。
"""

from __future__ import annotations

import sys

from chat2todo.qt.main import main

if __name__ == "__main__":
    sys.exit(main())
