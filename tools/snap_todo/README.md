# 截图待办（snap_todo）

可选插件：把截图里的文字变成待办。补充「导入文件」之外的快捷入口——看到要办的事，随手一截就入库。

## 用法

1. **Win+Shift+S** 框选截图（图片自动进剪贴板）
2. 双击 `snap.bat`，或从控制台点「截图待办」
3. 弹窗里确认识别文字（可直接改），点「提取入库」

识别出的每一行作为一条消息走核心管线（抽取 → 语义清洗 → AI 复核 → 入库），session 记为 `snap`。

## 全局热键（可选）

双击 `截图热键.bat` 后常驻后台，任何时候 **Ctrl+Alt+S** 直接唤出确认窗（仍需先 Win+Shift+S 截图）。
取消方法：任务管理器结束对应 `pyw.exe` 进程。热键被占用时会弹提示。

## 识别引擎（自动选择）

| 引擎 | 条件 | 特点 |
| ---- | ---- | ---- |
| Windows 自带 OCR | 默认 | 离线、中文好、零安装（winsdk 在则直调，否则走 PowerShell WinRT 桥） |
| Ollama 视觉模型 | 配置后自动升级 | 看图直提，只输出待办相关句子 |

启用视觉模型：

```sh
ollama pull qwen2.5vl:7b
```

然后在 `%USERPROFILE%\Chat2Todo\c2t.toml` 追加：

```toml
[snap]
vision_model = "qwen2.5vl:7b"
```

`vision_model` 留空（或模型未拉取）时自动回退 Windows OCR。

## 命令行参数

```sh
py -3 snap.py [--db 路径] [--engine auto|win|vision] [--watch] [--interval 秒]
```

- 默认只处理一次当前剪贴板；`--watch` 持续监视，每截一张图自动识别并弹确认窗，Ctrl+C 退出
- `--db` 默认 `%USERPROFILE%\Chat2Todo\qq.db`（与控制台一致）

## 依赖

仅 Python 标准库；核心包零依赖承诺不变。识别用系统能力（WinRT OCR）或本机 Ollama，不引入第三方包。
