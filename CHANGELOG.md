# 更新日志

## [0.1.0] - 2026-10-23

首个可运行版本。

### 新增

- QQ 导出 txt 适配器（兼容手机 / 电脑端两种行头）
- ChatLab Format JSON / JSONL 适配器（可双向互换）
- 幂等去重：时间戳 + 发送者 + 内容长度 + 内容哈希
- 可解释的待办抽取：行动词 / 时间词 / 指向我 / 疑问 / 交易，命中即给理由与置信度
- SQLite 存储（messages / todos 两张表，UNIQUE 约束保证重复导入安全）
- Markdown 输出，可直接放入 Obsidian
- CLI：`import` / `todos` / `export-md` / `stats` / `done` / `adapters`
- 9 个单元测试，覆盖解析、去重、抽取、存储、渲染
- GitHub Actions：Linux / Windows / macOS × Python 3.9 / 3.11 / 3.12

### 明确不做

- 不读客户端数据库、不读进程内存、不注入、不抓包
- 不做实时监听、不做群发

### 已知限制

- 规则引擎会误判含「记得」的陈述句，需人工复核（`status=unconfirmed`）
- 尚未支持图片 / 语音的内容解析，仅识别类型
