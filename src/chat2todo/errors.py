"""统一错误码。

对应文档第九章的错误码表：每个错误都带一句可执行的处理建议，
而不是只抛一个 traceback 给用户。
"""

from __future__ import annotations

CODES = {
    "E1001": "文件不存在或编码无法识别",
    "E1002": "未匹配到任何适配器",
    "E2001": "数据库被占用",
    "E3001": "导出目录不可写",
    "E4001": "未找到 QQ 数据库",
    "E4002": "数据库后端未配置",
    "E4003": "数据库解密失败",
    "E4004": "社区解密模块不可用",
    "E9001": "内部未知错误",
}

HINTS = {
    "E1001": "确认路径是否正确；GBK 文件加 --encoding gbk",
    "E1002": "运行 c2t adapters 查看已支持的格式",
    "E2001": "关闭其他正在使用该库的实例，或稍等锁释放",
    "E3001": "换一个目录，或检查写权限",
    "E4001": "用 [db].path 指定 nt_msg.db，或改用导出文件导入",
    "E4002": "配置 [db].module 指向社区解密工具，或 [db].key 提供密钥",
    "E4003": "改用社区模块，或先用导出文件路线",
    "E4004": "检查 [db].module 路径与 MESSAGE 格式",
    "E9001": "附上日志提 issue",
}


class C2TError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {CODES.get(code, '未知错误')}"
                         + (f"：{detail}" if detail else ""))

    @property
    def hint(self) -> str:
        return HINTS.get(self.code, "")

    def render(self) -> str:
        msg = f"[{self.code}] {CODES.get(self.code, '未知错误')}"
        if self.detail:
            msg += f"\n  细节：{self.detail}"
        if self.hint:
            msg += f"\n  建议：{self.hint}"
        return msg
