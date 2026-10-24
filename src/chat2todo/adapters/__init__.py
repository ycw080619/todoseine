"""数据源适配器。"""

from .base import SourceAdapter  # noqa: F401
from .chatlab_json import ChatLabJsonAdapter  # noqa: F401
from .qq_txt import QQTxtAdapter  # noqa: F401

REGISTRY = {
    "qq_txt": QQTxtAdapter,
    "chatlab_json": ChatLabJsonAdapter,
}


def get_adapter(name: str) -> "SourceAdapter":
    try:
        return REGISTRY[name]()
    except KeyError:
        raise ValueError(
            f"未知适配器: {name}（可用: {', '.join(sorted(REGISTRY))}）"
        ) from None
