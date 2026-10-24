"""打包：把源码压成单文件 .pyz（标准库 zipapp，不引入任何第三方依赖）。

用法：
    python tools/build_pyz.py
    python dist/c2t.pyz --help

说明：
- .pyz 只需要目标机器有 Python 3.9+，不需要 pip install；
- 想要脱离 Python 运行（单个 exe），需要另外装 PyInstaller，属可选步骤，
  不在零依赖承诺范围内，详见 docs/packaging.md。
"""

from __future__ import annotations

import shutil
import sys
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DIST = ROOT / "dist"


def main() -> int:
    if not (SRC / "chat2todo" / "__init__.py").exists():
        print(f"[E3001] 找不到源码目录：{SRC}", file=sys.stderr)
        return 1

    DIST.mkdir(parents=True, exist_ok=True)
    target = DIST / "c2t.pyz"
    if target.exists():
        target.unlink()

    zipapp.create_archive(
        SRC,
        target,
        main="chat2todo.cli:main",
        compressed=True,
    )
    size = target.stat().st_size
    print(f"built  {target}")
    print(f"size   {size} bytes")
    print(f"run    {sys.executable} {target} --help")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
