# TodoSeine 插件 SDK（v1）

插件是**独立目录**里的一组文件，由 TodoSeine 用子进程方式调用。核心程序不会
`import` 插件代码，因此插件崩溃、死循环、内存泄漏都只影响插件自己。

> 半自动转发插件由独立仓库发布，**不随主包捆绑**，也不在本 SDK 的示例范围内。

## 1. 目录结构

```
%USERPROFILE%\.chat2todo\plugins\
└── hello\
    ├── manifest.json      # 必填，见下
    └── entry.py           # 入口脚本，manifest.entry 指定
```

也可以用 `PluginManager(folder=...)` 指向任意目录（CLI 的 `plugin-status` 默认读
`~/.chat2todo/plugins`）。

## 2. manifest.json

```json
{
  "schema": "chat2todo-plugin-v1",
  "id": "hello",
  "name": "示例插件",
  "version": "0.1.0",
  "author": "you",
  "description": "演示最小插件",
  "permission_level": "file",
  "entry": "entry.py",
  "capabilities": ["notify"],
  "permissions": ["messages.read"]
}
```

| 字段 | 说明 |
| --- | --- |
| `schema` | 留空或 `chat2todo-plugin-v1`；写别的值直接拒载 |
| `id` | 小写 snake_case，且必须是合法 Python 标识符，全局唯一 |
| `name` / `version` | 必填，展示与控制台列表用 |
| `permission_level` | `file` / `db` / `uia`，与适配器基类一致 |
| `entry` | 相对 manifest 的入口脚本，默认 `entry.py` |
| `permissions` | 运行时事件权限白名单，见第 4 节；未声明的直接拒绝 |

## 3. 调用协议

宿主以 `python -I <entry>` 启动子进程，当前目录是插件目录，并通过环境变量传入事件：

| 环境变量 | 内容 |
| --- | --- |
| `C2T_EVENT` | JSON：`{"event": "message.new", "payload": {...}}` |
| `C2T_PLUGIN_ID` | 插件 id |

插件把结果 JSON **打到 stdout 最后一行**；其余输出会被忽略。退出码非 0、超时
（默认 10 秒）或抛异常都算一次崩溃。

最小示例：

```python
# entry.py
import json, os

event = json.loads(os.environ["C2T_EVENT"])
print(json.dumps({"received": event["event"], "size": len(event["payload"])},
                 ensure_ascii=False))
```

## 4. 权限白名单

| 事件 | 需要的权限 |
| --- | --- |
| `message.new` | `messages.read` |
| `todo.created` / `todo.updated` / `todo.completed` | `todos.read` |
| `notify` | `notify.write` |

调用未声明权限的事件会抛 `PluginError`，不会启动子进程。

## 5. 崩溃、安全模式与回滚

- 每个插件在 7 天窗口内累计 **2 次崩溃** → 全部插件进入 **安全模式**（禁用所有第三方插件，
  记录进入前的启用列表）；
- 安全模式下不能启用插件；`c2t plugin-rollback` 或双击 `tools/回滚插件.bat` 会恢复到
  最近一次快照并退出安全模式；
- 启用插件时自动打包快照（`plugins/backup-YYYYmmdd-HHMMSS.zip`，含插件目录与配置文件），
  默认保留 5 份。

常用命令：

```bash
c2t plugin-status           # 启用状态、7 天崩溃次数、快照列表
c2t plugin-enable hello     # 启用（自动打快照）
c2t plugin-disable hello    # 停用
c2t plugin-rollback         # 回滚 + 退出安全模式
```

## 6. 开发建议

- 不要在插件里写死 `todoseine` 的私有路径；需要数据时读环境变量或用宿主给定的事件负载；
- 插件只应做「读事件 → 计算 → 打一行 JSON」，耗时逻辑请自行放进后台线程并在超时前返回；
- 涉及个人数据的日志不要直接打印，宿主侧的脱敏规则管不到子进程的 stdout；
- 插件目录不要放 `*.pyc`、虚拟环境或大型模型文件，快照会把这些一起打包。
