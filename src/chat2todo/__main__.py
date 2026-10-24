"""允许 `python -m chat2todo` 直接运行。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
