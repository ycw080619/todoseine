# Chat2Todo

**本地优先的聊天记录 → 待办提取器。** 零依赖开箱即用 · 仅适配 Windows 11 · AI / 截图待办 / 桌面看板均为可选增强。

> Local-first chat-to-todo extractor. Zero-dependency core, offline by default. Windows 11 only.

![Chat2Todo 桌面看板：待办 / 待定 / 今日 / 完成 四个标签，右侧显示到期时间](docs/dashboard.png)

---

## 它解决什么问题

群聊里 80% 是闲聊，真正需要你动手的只有几条：「明天下午三点前把表发给老王」「@你 记得报名」。
Chat2Todo 把这些话自动挑出来，汇成一份待办清单，放在桌面看板上。它只回答一个问题：**还需要我做什么？**

它**不**分析社交关系、**不**做词云、**不**联网上传、**不**修改 QQ 任何东西（纯只读）。

---

## 3 步上手（小白路径）

1. 安装 Python 3.9+（勾选 Add to PATH）
2. 双击 `tools/qq_db_export/console.bat` 打开控制台
3. 点「设置」填一次 QQ 数据库口令（DPAPI 加密保存）→ 点「立即同步」→ 点「打开看板」

不想读 QQ 数据库？点「导入文件」选一份导出的 txt / JSON 也能用，零配置。

---

## 用户分层

| 层级 | 你是谁 | 你用到的能力 |
|---|---|---|
| 纯小白 | 双击就用 | 控制台：立即同步 / 看板 / 导入文件 |
| 普通用户 | 想更省事 | 截图待办（Win+Shift+S + 按钮）、Ctrl+Alt+S 全局热键、定时同步、桌面监视 |
| 进阶玩家 | 本机有 Ollama | AI 复核/意图/改写（qwen2.5）、语义去重（bge-m3）、截图看图直提（qwen2.5vl） |
| 命令行用户 | 脚本化集成 | `c2t` 全套子命令 + `C2T_*` 环境变量 |
| 开发者 | 想贡献代码 | 适配器 / tools 插件范式，103 项 pytest 护航 |

完整的分层设计与上手流程见 [分层方案.md](分层方案.md)；非技术介绍见 [项目说明.md](项目说明.md)。

---

## 特性

- **零依赖核心**：纯 Python 标准库，核心包永不引入第三方依赖
- **三条采集链路**：QQ 数据库只读直取 / 导出文件导入 / 剪贴板截图识别，汇入同一套抽取管线
- **可解释规则**：每条待办附带判定理由与置信度；≥4 分判待办、3 分待确认
- **可选 AI 复核**：本地 Ollama（免费）或云端 OpenAI 兼容 API（DeepSeek / 豆包 / 通义千问 / OpenAI GPT·Codex / Claude·OpenRouter 预设一键填）；不可用时静默退回规则结果，不丢数据
- **双引擎截图识别**：默认 Windows 自带 OCR（离线零安装），配置视觉模型后自动升级
- **桌面看板**：玻璃质感悬浮窗，待办/待定/今日/完成四标签、底部回车快速添加、滚轮翻页、Ctrl+滚轮调透明度、可收起成小时钟胶囊、可置顶、支持撤销（Ctrl+Z）与右键菜单
- **幂等入库**：重复导入不产生重复数据
- **开放格式**：内部模型与 ChatLab Format 对齐，可互导
- **隐私守门**：口令 DPAPI 加密存放；QQ 库纯只读；默认零网络请求（除 127.0.0.1 本机 Ollama）

---

## 命令行速览

```bash
pip install -e .            # 或免安装：PYTHONPATH=src python -m chat2todo ...

c2t import chat.txt --db qq.db      # 导入导出文件（txt / ChatLab JSON）
c2t db-key --save                   # 保存 QQ 库口令（DPAPI 加密）
c2t db                              # 数据库通道自检：定位 / 引擎 / 试读
c2t db-import --days 7 --db qq.db   # 只读拉取最近 7 天消息
c2t todos / done / search / show    # 查看 / 完成 / 检索 / 上下文
c2t export --format md --out out    # 导出 md / csv / json
c2t dash --db qq.db                 # 悬浮待办看板
c2t doctor                          # 环境自检
```

截图待办（独立插件，tools/snap_todo/）：

```bash
snap.py                 # 处理一次剪贴板截图
snap.py --hotkey        # 常驻：Ctrl+Alt+S 唤出
snap.py --watch         # 监视剪贴板，截一张处理一张
```

---

## 数据流

```
QQ 数据库（只读）     导出文件          剪贴板截图
   db-import          import       OCR / 视觉模型 + 人工确认
      └────────┬─────────┴─────────────┘
               ▼
     去重 → 规则打分 → 语义清洗 → AI 复核（可选）
               ▼
        SQLite 单库 qq.db
               ▼
   悬浮看板 / 控制台统计 / 导出 md·csv·json
```

---

## 如何导出聊天记录

| 平台 | 获取方式 | 适配器 |
|---|---|---|
| QQ | 「聊天记录」→ 导出 txt；或用数据库只读通道免导出 | `qq_txt` / `db-import` |
| 微信 | 第三方工具导出的 txt（三种行头自动识别） | `wechat_txt` |
| 通用 | ChatLab Format JSON / JSONL | `chatlab_json` |

导出建议：**脱敏后再测试**；提交 issue 请勿附真实聊天内容。

---

## 路线图

- [x] v0.1 QQ txt 导入、去重、规则抽取、SQLite、Markdown
- [x] v0.3 微信 txt 适配器
- [x] v0.4 QQ NT 数据库只读通道（sqlcipher3 + DPAPI 密钥保管）、语义清洗（bge-m3）、LLM 复核/意图/改写（Ollama）
- [x] 2026-10 控制台 GUI、悬浮看板、定时同步、截图待办插件（Windows OCR + 视觉模型双引擎）、全局热键
- [ ] 第三方插件注册协议（manifest / 权限声明）
- [ ] 本地 API 与 MCP 接口
- [ ] 多操作系统适配（当前仅 Win11）

---

## 安全与合规

- 只处理**你本人有权访问**的聊天记录；数据库通道纯只读，不注入、不抓包、不模拟登录
- 不提供、不接受任何注入/抓包/模拟登录相关的代码或 PR；口令提取工具不随本项目分发
- 不做实时监听、不做后台抓取、不做群发
- 涉及他人隐私请自行取得知情同意；分析结果不得作为法律证据或决策依据

详见 [SECURITY.md](SECURITY.md)。

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。最需要**新平台适配器**与 **tools/ 插件**。注意：核心包零第三方依赖是硬约束，可选能力请放插件层。

## 许可证

MIT
