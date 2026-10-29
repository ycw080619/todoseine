# Hermes 技能包（可选）

Hermes 是给「外部助手 / 自动化脚本」用的一层最小运维接口：查状态、看巡检结果、
收今日汇总、代用户回写待办。它不联网、不暴露公网，只绑 `127.0.0.1`。

## 启动

```bash
c2t hermes-serve              # 默认随机端口，写入 ~/.chat2todo/hermes/endpoint.json
c2t hermes-token              # 查看 token（--rotate 轮换）
c2t hermes-summary --out 今日.md
```

Qt 版在「设置 → 联动 → Hermes」里开关，启动后会把地址写进设置页，可直接复制 token。

## 鉴权

- token 存 `~/.chat2todo/hermes/token.bin`，DPAPI 加密（失败降级为 600 权限明文）
- 请求头 `Authorization: Bearer <token>`，或 `?token=<token>`
- 非本机连接一律拒绝；Host / Origin 必须是本机；Body 上限 8 KB
- 轮换后旧 token 立即失效，重启服务生效

## 接口

`GET` 三个只读端点：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/status` | 采集是否在跑、消息数、待办数、AI 是否配置 |
| GET | `/todos` | 待办列表（脱敏后，与桌面联动的 `todos.json` 同源） |
| GET | `/summary` | 当日汇总 Markdown 文本 |

`POST /command` 是唯一的写入口，Body 必须是一个对象，`command` 字段选动作：

```json
{"command": "done", "id": 12}
```

| command | 附加字段 | 说明 |
|---|---|---|
| `status` | — | 同 `GET /status` |
| `inspect` | — | 巡检：发现问题并自动重启采集，返回 `{issues, restarted, status, at}` |
| `restart` | — | 重启采集进程 |
| `todos` | — | 同 `GET /todos` |
| `summary` | — | 同 `GET /summary` |
| `add` | `text` | 新增待办（来源记为 `Hermes`） |
| `done` | `id` | 标记完成 |
| `pending` | `id` | 恢复为待办 |
| `edit` | `id`, `text` | 改文本 |
| `delete` | `id` | 移入回收站 |
| `forward` | — | 半自动转发：当前固定返回「功能未启用」 |

成功返回 `200` + 结果对象；参数缺失、未知指令返回 `400` + `{"error": "..."}`。

所有回写走的是与 Qt 看板相同的 `Store` 逻辑，因此会触发桌面联动导出、
看板刷新与校准反馈，不存在「接口改了库里却看不到」的情况。

## 技能包写法

想把它包成自己的技能，只需要一个 `SKILL.md` + 两个约定：

1. **先读 token**：从 `~/.chat2todo/hermes/token.bin` 取（或让用户在设置页复制）；
2. **只走回环**：所有请求发往 `http://127.0.0.1:<port>`，端口从
   `endpoint.json` 读，不要硬编码。

示例（伪代码）：

```text
读取 endpoint.json → port
读取 token（用户提供或本地文件）
GET  /status                      → 采集是否正常；不正常再 POST /command {"command":"restart"}
GET  /summary                     → 生成「今天还剩什么」摘要
POST /command {"command":"done","id":12}   → 用户说「这条做完了」时回写
```

## 边界

- 不做群发、不模拟登录、不读 QQ 存储文件
- `/todos` 与 `/summary` 默认走 `redact_for_archive`；AI 明文开关不会影响这里（始终脱敏）
- `forward` 只是占位：真正转发消息属于高风险动作，本版本不自动发送
