# QQ 数据库导出（可选工具）

把本机 QQ NT 的聊天库解成 JSONL，交给 Chat2Todo 的数据库通道或离线分析。

> ⚠️ 只处理**你自己**的聊天记录。产物含真实私聊内容，用完请删除。
> 本目录脚本不随核心包安装，属于「进阶工具」，小白用户可以完全忽略。

## 为什么需要 Docker

`nt_msg.db` 是 **SQLCipher 4 加密库**，外面套了 1024 字节自定义头。
Windows 上没有现成的 `sqlcipher3` 轮子，而 Debian 容器里 `apt install sqlcipher` 一条命令就能拿到引擎。
所以这里用「容器内解密 + 导出」，宿主机不装任何东西。

## 前置

1. 已提取数据库口令（社区工具 `ntqq_get_key.ps1` 一类，需账号处于离线状态才能取）
2. Windows 上装好 Docker Desktop（Linux 容器）

## 用法

```bash
# 1. 先把 nt_msg.db 复制一份到工作目录（不要直接对原库操作）
#    路径通常是 ...\Tencent Files\<QQ号>\nt_qq\nt_db\

# 2. 复制本目录的三个文件到同一工作目录：run.sh / export.py / nt_msg.db
# 3. 执行（把 <KEY> 换成你的口令）
docker run --rm \
  --dns 114.114.114.114 --dns 8.8.8.8 \
  -e NTQQ_DB_KEY='<KEY>' \
  -e DAYS=7 \
  -v "$PWD":/data -v "$PWD":/src:ro \
  python:3.11-slim \
  bash /src/run.sh
```

产物：`messages.jsonl`，每行一条消息：

```json
{"timestamp":1792842363,"sender":"231458000","sender_name":"小李","content":"明天前把周报发我","type":0,"chat_id":"670649801730901864","msg_id":"..."}
```

## 字段说明

| 来源列 | 含义 | 说明 |
| --- | --- | --- |
| `40001` | msgId | 64 位整数，去重可用 |
| `40002` | 会话 ID | 群号或对方 QQ |
| `40021` / `40027` | 发送者 QQ | 两个都试，取非空 |
| `40050` | Unix 时间戳 | 秒 |
| `40093` | 发送者昵称 | **不是**正文 |
| `40800` | 消息体 | protobuf BLOB，中文正文在其中 |

正文没有公开的 proto 定义，`export.py` 的做法是：按 UTF-8 解码后取最长的可打印片段，
再剥掉开头粘连的类型残渣（例如 `6照片记得…` 里的 `6`）。

## 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `Temporary failure resolving deb.debian.org` | 容器没 DNS | `--dns` 同时给 `114.114.114.114` 与 `8.8.8.8`，只给一个常常失败 |
| `sqlcipher: command not found` | 装与用在两个容器里 | 必须同一次 `docker run` 内完成 |
| `table c2c_msg_table already exists` | 上次的 plain.db 还在 | 脚本已 `rm -f`，手工执行时也要先删 |
| 打开后报 `file is not a database` | 没剥 1024 字节头 | 用 `tail -c +1025` |
| 提示 `1st ORDER BY term out of range` | 数字列名被当成序号 | 列名加双引号：`"40012"` |

## 安全提示

- 明文库（`plain.db`）与消息 JSONL 都含真实聊天内容，属于敏感文件；
- 脚本结束时会删掉 `plain.db` / `clear.db` / `dec.sql`，但 **JSONL 会保留**，请自行决定保存多久；
- 不要把明文库、口令、JSONL 提交到任何仓库。
