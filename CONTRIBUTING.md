# 贡献指南

欢迎 PR，尤其是**新平台适配器**和**脱敏样本**。

## 我们不接受的内容

以下方向的 PR 会被直接关闭，请勿提交：

- 破解、解密第三方客户端数据库
- 读取其它进程内存、注入 hook、修改客户端
- 抓包、模拟登录、协议逆向
- 实时监听、后台常驻抓取、群发

这些不是能力问题，是**项目定位与法律边界**问题。Chat2Todo 只处理用户自己合法导出的文件。

## 开发环境

```bash
git clone https://github.com/<you>/chat2todo.git
cd chat2todo
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m unittest discover -s tests -v
ruff check src tests
```

## 提交前检查

1. 测试全绿：`python -m unittest discover -s tests`
2. 风格：`ruff check src tests`
3. 不引入运行时第三方依赖（核心必须保持标准库）
4. 不提交真实聊天记录

## 新增一个适配器

```python
from chat2todo.adapters.base import SourceAdapter
from chat2todo.models import Message

class MyPlatformAdapter(SourceAdapter):
    name = "my_platform"
    extensions = (".txt",)

    def read(self, path):
        for line in self.read_lines(path):
            ...
            yield Message(timestamp=..., sender_id=..., sender_name=..., content=...)
```

然后在 `adapters/__init__.py` 的 `REGISTRY` 里注册，并在 `tests/` 加一个基于脱敏样本的测试。

## 提交信息

采用 Conventional Commits：`feat:`, `fix:`, `docs:`, `test:`, `refactor:`。
