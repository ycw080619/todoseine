"""适配器基类。

新增一个聊天平台 = 继承 SourceAdapter 并实现 read()。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Iterable, List

from ..models import Message


class SourceAdapter(ABC):
    """把一个导出文件转换成统一的 Message 序列。

    新增平台 = 继承 SourceAdapter 并实现 read()；第三方贡献者写适配器时
    只需实现这一个方法 + 两个类属性，然后通过 entry points 注册。
    """

    #: 适配器标识，CLI 的 --adapter 用它选择
    name: str = "base"
    #: 支持的文件后缀（小写，含点）
    extensions: tuple = ()
    #: 权限级别：'file'（纯文本导出，低权限）/ 'db'（读本机聊天数据库，中权限）/ 'uia'（UI 自动化，高权限）
    #: UI 层可以据此给用户展示"这个适配器需要管理员权限吗"之类的提示
    permission_level: str = "file"

    @abstractmethod
    def read(self, path: str) -> Iterable[Message]:
        """读取文件并产出 Message。实现应当是流式的，避免一次性载入大文件。"""
        raise NotImplementedError

    # --- 工具方法 -------------------------------------------------
    @staticmethod
    def read_lines(path: str) -> List[str]:
        """按常见编码尝试读取文本，返回行列表。"""
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
            try:
                with open(path, "r", encoding=encoding) as fh:
                    return fh.read().splitlines()
            except (UnicodeDecodeError, UnicodeError):
                continue
        raise UnicodeDecodeError(
            "unknown", b"", 0, 1, f"无法识别 {os.path.basename(path)} 的文本编码"
        )
