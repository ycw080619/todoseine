# Chat2Todo

**本地优先的聊天记录 → 待办提取器。**
把你自己导出的聊天记录，变成一份可执行的待办清单。全程离线，不联网，不上传。

> Local-first chat-to-todo extractor. Turn your own exported chat logs into an actionable task list. Fully offline.

---

## 它解决什么问题

群聊里 80% 是闲聊，真正需要你动手的只有几条：
「明天下午三点前把表发给老王」「这个方案你确认下」「@你 记得报名」。

你可能一整天在几百条消息里翻这几句。Chat2Todo 做的就是：
**导入导出的聊天记录 → 归一化 → 去重 → 抽取出行动项 → 输出 Markdown / SQLite / 本地 API。**

它不分析你的社交关系，也不做词云。它只回答一个问题：**还需要我做什么？**

---

## 为什么不直接读数据库

因为那需要破解客户端加密、读取进程内存、注入 hook。这类工具：
- 违反平台用户协议，有账号风险；
- 版本一变就失效；
- 一旦开源分发，几乎必然收到法务函。

Chat2Todo 的边界很明确：**只吃你已经合法导出的文件**。
项目不提供、也不接受任何解密、注入、抓包相关的 PR。

---

## 特性

- **零依赖核心**：纯 Python 标准库，`pip install` 后即可用，不拉任何第三方包
- **开放格式**：内部统一为 ChatLab 兼容的 JSON/JSONL，可与其它工具互通
- **幂等去重**：同一份记录重复导入不会产生重复数据
- **规则可解释**：每条待办都附带「为什么被判定为待办」的理由和置信度
- **可插拔适配器**：新增一个聊天平台 = 写一个 `SourceAdapter` 子类
- **本地存储**：SQLite 单文件，随时可删，随时可备份
- **可选本地 API**：`127.0.0.1` + Bearer Token，供脚本/AI 查询
- **不联网**：默认不发任何网络请求

---

## 快速开始

```bash
git clone https://github.com/<you>/chat2todo.git
cd chat2todo
pip install -e .

# 导入一份 QQ 导出的聊天记录
c2t import examples/sample_qq_export.txt --db c2t.db --out out

# 查看待办
c2t todos --db c2t.db

# 导出为 Markdown（可直接放进 Obsidian）
c2t export-md --db c2t.db --out out/todos.md
```

零安装试用（不装包也能跑）：

```bash
PYTHONPATH=src python -m chat2todo import examples/sample_qq_export.txt --db c2t.db
```

---

## 如何导出聊天记录

各平台导出方式不同，本项目只负责「解析已经导出的文件」。

| 平台 | 获取方式 | 适配器 |
|---|---|---|
| QQ | 手机/电脑端「聊天记录」→ 导出为 txt | `qq_txt`（内置） |
| 通用 | 任何符合 ChatLab Format 的 JSON / JSONL | `chatlab_json`（内置） |
| 其它 | 欢迎提交 PR | — |

导出建议：**脱敏后再测试**。提交 issue 时请勿附真实聊天内容。

---

## 数据流

```
导出文件
   │  SourceAdapter（qq_txt / chatlab_json / 你的适配器）
   ▼
Message 列表 ──► 去重（时间戳+发送者+长度+内容哈希）
   │
   ▼
分类（闲聊 / 信息 / 风险 / 待办）
   │
   ▼
待办抽取（规则 + 可选 LLM）
   │
   ├──► SQLite（结构化查询）
   ├──► Markdown（Obsidian 镜像）
   └──► 本地 API（127.0.0.1，可选）
```

---

## 数据格式

内部消息模型（与 [ChatLab Format](https://docs.chatlab.fun/standard/chatlab-format) 对齐，可直接互导）：

```json
{
  "sender": "123456",
  "accountName": "老王",
  "timestamp": 1703001600,
  "type": 0,
  "content": "明天三点前把表发我"
}
```

消息类型：`0` 文本、`1` 图片、`2` 语音、`3` 视频、`4` 文件、`5` 表情、`7` 链接、`20` 红包、`21` 转账、`80` 系统、`81` 撤回、`99` 其他。

去重键：`(时间戳, 发送者ID, 内容长度, 内容哈希)`。
前三位与 ChatLab 一致，第四位用于区分「同一秒同一个人发的两条等长但不同内容的消息」。

---

## 待办判定规则

当前版本是**可解释的规则引擎**，不是黑盒模型。命中以下特征会计分：

| 特征 | 权重 | 例子 |
|---|---|---|
| 行动词 | 3 | 记得 / 别忘 / 安排 / 确认 / 提交 / 报名 |
| 时间词 | 3 | 明天 / 下周一 / 三点前 / 截止 |
| 指派给「我」 | 2 | @我 / 你来做 / 麻烦你 |
| 疑问句 | 1 | 能不能 / 可以吗 / 行不行 |
| 金额/链接 | 1 | 转账 / 付款链接 |

总分 ≥ 4 判为待办；3 分为「待确认」。阈值可在 `config.py` 调整。

每条待办会记录 `reason`，例如：`行动词:提交; 时间词:明天; @我`。

---

## 路线图

- [x] v0.1 QQ txt 导入、去重、规则抽取、SQLite、Markdown
- [ ] v0.2 微信/Telegram 适配器
- [ ] v0.3 LLM 二次判定（可选、可关闭、可本地模型）
- [ ] v0.4 本地 API 与 MCP 接口
- [ ] v0.5 桌面托盘提醒

---

## 安全与合规

- 本项目**只处理你本人有权访问的聊天记录**。
- 不提供、不接受任何**解密、注入、抓包、模拟登录**相关的代码或 PR。
- 不做实时监听、不做后台抓取、不做群发。
- 涉及他人隐私时，请自行取得知情同意。
- 分析结果可能出错，不得作为法律证据或决策依据。
- 因使用第三方平台数据导致的账号限制，与本项目无关。

详见 [SECURITY.md](SECURITY.md)。

---

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。最需要的是**新平台适配器**和**脱敏样本**。

## 许可证

MIT
