# 数据库只读通道（可选）

TodoSeine 的主路线是「导入导出文件」，不依赖任何客户端内部数据。
数据库只读通道是**可选项**：给想跳过手动导出的用户用，默认关闭。

> 只读取**你自己账号**的本地数据。全程只读：不写 QQ 目录、不改原库、不注入进程。

## 快速上手

```bash
pip install todoseine[db]   # 装上 sqlcipher3 引擎（Windows 预编译轮，免编译免 Docker）
c2t db-key --save           # 第一次：交互输入口令，校验通过后 DPAPI 加密存本机
c2t db                      # 自检：库位置 / 引擎 / 密钥试读
c2t db-import --days 7      # 只读拉取最近 7 天消息并抽取待办
```

不再需要 Docker、不再产生明文库文件——解密在内存中完成，临时快照用完即删。

## 口令从哪来

`nt_msg.db` 的口令（16 位）**不保存在磁盘上**——它是 QQ 运行时在进程内存里现算的。
因此提取工具都要求 **QQ 保持登录运行状态**，
从进程内存中扫描出候选口令并逐一试解验证。提取是一次性的；读库本身不用关 QQ（程序会先拍快照）。
本项目不附带、不分发提取工具，可自行获取：

- [QQBackup/qq-win-db-key](https://github.com/QQBackup/qq-win-db-key)
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)
- [QQ-MCP-Server](https://github.com/print-yuhuan/QQ-MCP-Server)

拿到口令后的存放优先级（`resolve_key` 按此顺序找）：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | `[db].key` 配置项 | 明文，不推荐 |
| 2 | 环境变量 `C2T_DB_KEY` | 适合 CI / 临时使用 |
| 3 | `[db].key_file` 指向的文件 | 注意文件权限 |
| 4 | **`c2t db-key --save` 存的 DPAPI 密库** | 推荐：`~/Chat2Todo/dbkey.dat`，仅当前 Windows 用户可解 |

`db-key --save` 保存前会先用口令试解数据库首页（PBKDF2-HMAC-SHA512 / 4000 次），
解不开会拒存（`--force` 可强制）。老版本 sync.py 用 PowerShell SecureString 存的
口令文件会被自动识别读取，无需重新保存。

## 两条后端路径

| 后端 | 说明 | 需要什么 |
| --- | --- | --- |
| `builtin`（推荐） | 内置 sqlcipher3 引擎直读：快照 → 剥 1024 头 → PRAGMA 解密 → 40800 protobuf 解码 | `pip install todoseine[db]` + 口令 |
| `community` | 以子进程调用外部解密工具，读它的 JSONL 输出 | `[db].module` 指向可执行文件 |

两者都不可用时，`c2t db-import` 会给出 E4002 并提示下一步，不会中断其它功能。

## 配置

```toml
[db]
enabled  = true
path     = ""     # nt_msg.db 路径；留空自动探测
key      = ""     # 不推荐明文；优先 c2t db-key --save
key_file = ""
days     = 0      # db-import 默认时间窗，0 为全部
module   = ""     # 备用社区后端
args     = ""
```

环境变量：`C2T_DB_MODULE`、`C2T_DB_ARGS`、`C2T_DB_KEY`。

## 社区工具的输出协议

工具接收一个参数（数据库路径），向 stdout 输出 **JSONL**，每行一条消息：

```json
{"timestamp": 1761300000, "sender": "1001", "sender_name": "小李", "content": "明天下午三点前把周报发我", "type": 0}
```

字段名宽松兼容：`content`/`text`、`timestamp`/`ts`、`sender`/`sender_id`、`sender_name`/`name`。
退出码非 0 或输出为空都会被视为失败（E4004）。

## 技术要点

- 库布局：1024 字节自定义头 + 4096 字节页的标准 SQLCipher 4；
- PRAGMA 序列（顺序不能变）：`cipher_page_size=4096` → `key` → `kdf_iter=4000`
  → `HMAC_SHA1` / `PBKDF2_HMAC_SHA512`；
- 正文在 `40800` 列的 protobuf BLOB 里，由 `todoseine.core.pbbody` 的
  wire-format 解码器提取（无 proto 定义，按文档序拼接文本元素并过滤噪声）；
- 时间戳 `40050`（Unix 秒）、发送者 `40021`/`40027`、昵称 `40093`、会话 `40002`、msgId `40001`。

## 安全检查

- 全程只读：不写 QQ 目录、不改原库、不注入进程；
- 活库先拍快照再剥头，临时目录用完即删，明文不落盘；
- 自动探测只读注册表里的「文档」真实位置，避免被重定向后找错；
- 没有可用后端时不会静默假装成功，而是明确报错并给出下一步；
- 明文口令永不被打印、不写日志；DPAPI 密库只有本机当前用户能解开。
