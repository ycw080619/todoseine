# Windows 图形控制台（可选工具）

TodoSeine 的 Windows 桌面入口：一个窗口管住「同步 / 看板 / 导入 / 监视 / 定时 / 设置」。
数据通道是**内置引擎**（sqlcipher3 直读 QQ 数据库快照），不再需要 Docker。

> ⚠️ 只处理**你自己**的聊天记录。本目录脚本不随核心包安装，属于「进阶工具」，纯命令行用户可以完全忽略。

## 文件

| 文件 | 作用 |
| --- | --- |
| `console.bat` | 双击启动控制台（pyw 无窗格运行，路径自解析，免安装） |
| `console.py` | 控制台本体（tkinter，标准库实现） |
| `后台同步.bat` | 供 Windows 计划任务调用的无界面同步（`db-import --days 1`，日志追加到 `%USERPROFILE%\Chat2Todo\sync.log`） |

## 用法

1. 双击 `console.bat`；
2. 第一次使用：点「设置」填入数据库口令（DPAPI 加密存到 `%USERPROFILE%\Chat2Todo\dbkey.dat`，仅本机当前用户可解）；
3. 点「立即同步」：内置引擎快照解密 → 入库 → 规则/语义过滤 → 本地 Ollama 复核，日志实时滚动；
4. 「定时同步」按钮会注册 Windows 计划任务，每 2 小时后台跑一次 `后台同步.bat`。

## 前置

- Windows + Python 3.9+（自带 `py` 启动器）；
- `pip install sqlcipher3`（加密库引擎，排错见 [docs/db-channel.md](../../docs/db-channel.md)）；
- 数据库口令：用社区提取工具（QQBackup / NapCat 一类，QQ 需**保持登录运行**才能从内存扫到），
  在控制台「设置」里保存，或命令行 `c2t db-key --save`。

## 历史

v0.4 之前的「Docker 容器解密导出」链路（sync.py / export.py / pipeline.py / run.sh）已被内置后端取代并移除，
需要考古请翻 git 历史。
