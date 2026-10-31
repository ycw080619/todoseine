# 安全策略

## 设计前提

TodoSeine **只在本地处理用户自己的数据**：主链路为导出文件导入，进阶可选数据库只读通道。

- 不发起任何网络请求（`--api` 除外，且仅绑定 `127.0.0.1`；语义清洗仅访问本机 Ollama）
- 不上传、不遥测、不统计
- 不注入任何进程，不要求任何平台账号密码
- 数据库通道（可选）：只读你本人账号的本地 `nt_msg.db`，快照在内存中解密、明文不落盘；
  口令用 Windows DPAPI 加密存放（`~/Chat2Todo/dbkey.dat`），仅当前用户可解；
  密钥提取能力不随核心仓库分发，由用户主动勾选下载的官方独立插件提供。

## 数据建议

- 导出的聊天记录可能包含他人隐私，请自行评估并取得必要同意
- 测试、截图、issue 附件请先脱敏
- `.gitignore` 已排除 `*.db` 与 `out/`，但仍请勿把真实数据放在仓库目录

## 未签名运行提示

本项目分发的 `todoseine_qt.exe` **没有代码签名证书**（个人项目，签名证书年费较高且需要实名主体）。因此首次运行时会出现：

| 现象 | 原因 | 处理 |
|---|---|---|
| SmartScreen 蓝色提示「Windows 已保护你的电脑」 | 无签名 + 下载量低，未建立信誉 | 点「更多信息」→「仍要运行」 |
| Defender 报「Trojan:Win32/Wacatac」类告警 | 启发式误报，常见于 PyInstaller 打包的未签名 exe | 加入排除项，或自行从源码运行 |
| 浏览器下载被拦 | SmartScreen 文件信誉不足 | 保留文件后按上面的方式放行 |

**验证分发包未被篡改**：仓库 Release 页附 `SHA256SUMS.txt`，下载后执行

```powershell
Get-FileHash .\todoseine_qt.exe -Algorithm SHA256
```

与清单逐字比对，不一致就不要运行。

**不愿信任预编译 exe 的，建议直接从源码跑**（零第三方依赖即可启动 CLI）：

```powershell
git clone <repo> && cd todoseine
python -m venv .venv && .\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\c2t.exe --help
```

## 误报提交指引

收到杀毒软件告警时，按下面顺序处理，不要直接发 issue 说「有毒」：

1. 用上面 `Get-FileHash` 核对文件哈希是否与官方发布一致——**不一致说明是第三方二次打包，与本项目无关**
2. 一致仍被报毒：记录三个信息——杀软品牌与版本、告警名称（如 `Trojan:Win32/Wacatac.B!ml`）、文件哈希
3. 到该杀软厂商的误报提交页上传样本（微软 Defender 用 [Microsoft 安全情报提交](https://www.microsoft.com/en-us/wdsi/filesubmission)；火绒 / 360 / 卡巴各有申诉入口）
4. 同时在本仓库开 issue，标题写 `[误报] <杀软> <告警名>`，附上第 2 步的三项信息

厂商一般在 24~72 小时内解除误报；解除前可用源码方式运行，功能完全一致。

## 报告漏洞

发现安全问题时请**不要**公开 issue，发邮件到维护者信箱（在 GitHub 主页可见），48 小时内回复。

## 已知边界

| 风险 | 说明 |
|---|---|
| 解析错误 | 导出格式会随客户端版本变化，解析失败时请附脱敏样本提 issue |
| 误判 | 规则引擎会把「记得」这类词误判为待办，故每条都附 `reason` 与 `confidence` 供复核 |
| 隐私 | 数据库为明文 SQLite，请自行做好磁盘加密与访问控制 |
