## [Unreleased] - 2026-10-27

### 战略转折点：本地校准反馈闭环（用户专属壁垒）

- 新增 `feedback` 表 + `Store.record_feedback()/load_feedback_weights()/calibration_stats()/export_calibration()/import_calibration()`：每次用户点「标为非待办 / 标记完成 / 恢复」都自动写一条校准记录（mark_reviewed → feedback 表，零侵入）
- `load_feedback_weights()` 聚合出 `{(chat_id, sender_name): {reason_tag: adjustment}}`，adjustment ∈ [-0.5, 0.5]，样本数 < 3 过滤；**同标签在不同群权重独立**
- `extract.score_message(msg, weights_map=None)` 接权重——同群同 sender 上被 reject 过的 reason_tag 降分，被 confirm 过的升分；**db-import 入口自动 load_feedback_weights() 灌进 extract_todos()**，不需要用户手动干预
- 校准档案导出/导入：`schema: c2t-calibration-v1` 格式（项目专属），自带 chat_names；同 todo_id + action 重复导入自动跳过；schema 版本不匹配拒绝
- **校准闭环可观测性补齐**（此前 Store 有 5 个 feedback 方法但 console.py / cli.py 零引用）：
  - `Store.calibration_insights()` 新增：聚合 `calibration_stats + load_feedback_weights`，额外返回 active_groups（够 3 样本的来源组数）+ top_tags（|adj| 最大的 5 个 reason_tag）
  - 控制台「常规」页每周摘要后新增 🎯 校准反馈卡：累计/确认/拒绝/来源数/已生效组数 + 最大纠偏标签；导出/导入按钮（走 filedialog，schema 不匹配弹 warning）
  - CLI 新增 `c2t export-calibration <path>` / `c2t import-calibration <path>`
  - launcher.py `--help/-h` 早返回，windowed exe 不再弹 GUI
- **适配器基类 `SourceAdapter` 加 `permission_level` 字段**：`'file'`（纯文本导出）/ `'db'`（读本机聊天数据库）/ `'uia'`（UI 自动化）；给未来插件生态和 UIA 适配器预留权限分级接口

### 部署与分发：一键 exe 打包 + bat 自解析路径

- 新增 PyInstaller onefile windowed 打包：`tools/chat2todo-launcher.py` 单一入口同时路由控制台 GUI / CLI 子命令 / `-m chat2todo` 子进程调用 / 独立 `.py` 脚本（frozen 跳过、开发态 exec），**console.py 源码零改动**
- 新增 `tools/build.ps1`：Set-Location + Join-Path 绝对路径拼接（cwd 无关），try/finally 恢复调用者 cwd；pip 分拆显式检查 `$LASTEXITCODE`，解决 `$ErrorActionPreference=Stop` 把 stderr 日志（pip warning / PyInstaller INFO）误判为 NativeCommandError 的问题；实测一键产出 17.87 MB onefile windowed exe
- `.gitignore` 加 `*.spec`（PyInstaller spec 文件机器特定，不入库）
- 四个 .bat（后台同步 / dash_autostart / console / launch）路径自解析重写：**不再硬编码** `C:\Program Files\Python312\python.exe`，也不依赖 `py -3w`（py launcher 在某些机器 exit 103 找不到 pythonw runtime）；改为 `where pythonw` 拿绝对路径、找不到 fallback `where python`；Windows 计划任务和开机自启跨 Python 版本都能跑
- CI 收敛：Windows 11 × Python 3.12/3.13 + Ubuntu 冒烟；**加 pytest step**（原只跑 unittest discover 漏 36 个 fixture 测试），现 pytest 164 / unittest 128 全绿

### 看板每日早报三件套

- **每日早报 daily_mode**：每天首次启动自动切到卡片式摘要视图，`last_shown_date` 持久化到 `dash_pos.json`，当天不再重复切
- **每日 top-5 上限**：看板每日最多渲染 5 条待办（按 confidence 降序），剩余收在「展开」区，避免长列表淹没重点
- **周进度 footer**：看板底部新增 `· 本周清 N · 新增 M`，数据源 `Store.weekly_stats()` 纯查询方法，控制台「常规」页每周摘要卡复用同一函数

### 看板首次使用引导（用户体验 P0）

- **看板空状态按场景区分文案**：第一次打开（db 不存在）→ 👋「第一次使用 Chat2Todo → Ctrl+Shift+C 打开控制台」；db 有但 todos 为 0（没跑 sync）→ 「点 ⚙ 打开控制台立即同步，5 秒自动刷新」；有 todos 但当前筛选没匹配 → 通用引导。不再一律显示「暂无待办」让用户懵
- **控制台 PY 变量一致性确认**：`PY = sys.executable` 从 console.bat（用 pythonw.exe）启动时拿到的就是绝对路径，调子进程没问题；从命令行手动启动也没问题，跳过了不必要的一致性改动
- loop 轮询 30s → 5s：`refresh_ms` 固定 5000ms，render debounce 兜底——条目/筛选条件/库 mtime 全没变就整跳过，mtime 没变时 CPU 几乎零占用；同步完新待办最多 5s 就能看到
- mini ↔ 展开切换位置动画：原来瞬间切位置，现在启动 14 帧 ease_out_cubic 帧循环（与 _resize 的尺寸动画并行），约 220ms 整体过渡——同时动画位置和尺寸，视觉上更连贯
- render debounce：loop 里每次调 render() 都算一次 debounce hash（mini/tab/query/batch/pin/search + db mtime + 条目 signature 列表），和上次一样就整跳过，不 cv.delete("body") 不重画；库里没新待办时 CPU 几乎零占用
- 看板搜索结果命中关键词用强调色高亮（正文按「前缀 · 命中段 · 后缀」拆分画三段，中间 hit 用 ACCENT 色），视觉上一眼看到搜到啥
- 启动链路绝对路径加固：四个 .bat（后台同步 / dash_autostart / console / launch）全从 `py -3` 换成绝对 `C:\Program Files\Python312\python.exe` / `pythonw.exe`，各自加 `cd /d` 切仓库根目录；Windows 计划任务和开机自启不再依赖 py launcher 环境
- 「免打扰 / 特别关心」卡片新增按群名搜索添加：底部右侧下拉列出还不在名单里的已知群（群名（号码），可敲字过滤），选中即加入名单；已在名单里的不再出现在下拉中，群名后台补抓完成后下拉同步刷新
- 控制台「常规」页新增每周摘要卡：近 7 天新增 / 完成待办数（完成含当天勾掉的与回收站里已归档的）、最活跃群（`chat_names` 有群名显示群名，查不到用号码）；数据来自新增的 `Store.weekly_stats` 纯查询方法
- 看板新增搜索框：Ctrl+F 把顶部「添加待办」卡切换成搜索框，边敲边按「内容 / 发送人」大小写不敏感子串过滤当前标签页，Esc 或清空恢复原列表；纯过滤逻辑抽为 `dashboard.data.filter_items`；分段控件计数不受搜索影响；Ctrl+N 或进入批量模式自动退出搜索；无匹配时显示「没有匹配「关键词」的条目」
- 新增到期提醒：看板每轮刷新时检查未办条目的截止时间（复用 deadline 解析），1 小时内到期或刚过期的在屏幕右下角弹提醒卡片（最多列 3 条、8 秒自动消失、点击唤出看板，本次运行同一条只提醒一次）；`[dash] remind=false` 关闭、`remind_within` 调提前分钟数；修复拆包后三处函数内相对导入未加深（`from .config/.core` → `..`）的潜在运行期错误
- 看板单文件拆包：`dashboard.py`（约 1800 行）拆为 `chat2todo/dashboard/` 包——`app.py`（窗口与渲染）、`geom.py`（多显示器/吸附/锚点纯函数）、`win32.py`（单实例/热键/层级/毛玻璃）、`files.py`（文件名识别与定位）、`data.py`（数据层）；`__init__.py` 原样再导出全部名字，外部 import 路径与行为不变
- 群名映射自动补抓：控制台启动时若 `chat_names` 表为空，后台线程自动从 `group_info.db` 抓群名写库，完成后「免打扰 / 特别关心」两张卡片的号码即时换成群名显示（失败静默，不影响其他功能）
- `purge-chat --list` 的群名列改按显示宽度对齐（全角字符按 2 算），含中文群名时表列不再错位
- 控制台配置读取统一走 `chat2todo.config`（`_read` / `load` 显式传路径），删掉两处手写 TOML 解析；顺带修复快捷键与 `[snap] ai_refine` 读取未传配置路径、实际总拿默认值的问题
- 新增未标记消息清理：`Store.purge_unmarked / unmarked_counts`，CLI `c2t purge-unmarked [--dry-run]`，控制台「常规」页新增「未标记消息」卡片（显示条数 + 确认后清理）；只删 txt 导入的老消息，已抽出的待办保留

- 新增可选插件「截图待办」（tools/snap_todo/）：Win+Shift+S 截图 → Windows 自带 OCR（可选 Ollama 视觉模型直提）→ 确认弹窗 → 走核心管线入库；控制台新增「截图待办」按钮，弥补 MVP 导入不够快捷的问题
- 桌面看板增强：新增「待办 / 待定 / 今日 / 完成」四标签筛选、底部回车快速添加、滚轮翻页、Ctrl+滚轮调透明度、迷你时钟模式（点击展开）、置顶开关、撤销上一步（Ctrl+Z）与右键菜单（完成 / 非待办 / 恢复 / 看原文）；窗口高度随条目数自适应
- 新增深色模式：设置面板底部「深色模式」即时切换，控制台主窗口同步换肤（日志内容与运行状态保留），截图确认弹窗也从同一配置读取；主题写入 `c2t.toml` 的 `[ui] theme`，重启后保持
- 设置面板改用卡片式分组布局（AI 服务商 / 截图识别 / 数据库），顶部显示当前生效状态；新增「测试连接」按钮（本地查 /api/tags、云端查 /models，只读探测，成功后自动把可用模型填进视觉模型下拉）；服务商行直接列出国内与海外可选厂商
- 设置窗口支持接入云端大模型：内置 OpenAI 兼容服务商预设（本地 Ollama / DeepSeek / 豆包·火山方舟 / 通义千问·百炼 / OpenAI GPT·Codex / Claude·OpenRouter 中转 / 自定义），可选填 API Key；填 Key 保存后自动开启 AI 复核，本地 Ollama 免 Key（自动写占位 key）；TOML 布尔值正确落盘
- 控制台新增「截图热键」开关按钮；设置窗口可直接配置视觉模型（写入 [snap] 段）；确认弹窗显示识别引擎来源
- 控制台外观卡片新增「描边」「显示时间」开关（写回 `[ui] border` / `[ui] clock`，看板 2 秒内热重载）；修复控制台读外观配置时未传路径导致总是显示默认值，以及看板窗口尺寸变化后 `[ui] border` / `[ui] radius` 被写死成 1px / 18 的问题
- 新增按群清理历史：消息表补 `chat_id`（旧库自动迁移），`c2t db-backfill` 给历史消息回填来源群（不跑抽取、不调用 AI），`c2t purge-chat --list / --chat 123456 --dry-run / --chat 123456` 预览并清理指定群的消息及由它们抽出的待办（`--keep-todos` 只删消息）；同时 `db-import` 再次同步时会自动回填老记录缺的来源群
- 修复 DB 通道会话号取错列：`chat_id` 原取自 `40002`（msgRandom，每条消息都不同），实测确认群号/对方QQ 在 `40033`（群表=群号、私聊表=对方QQ 号，与特别关心样本群记录吻合）；已回填的错误数据已清空并用正确列重跑，历史消息已正确标记来源群
- 修复免打扰/特别关心过滤失效：这两个列表里的号码现在同时匹配「群号」与「发送者 QQ 号」。实测用户配置的免打扰项全部是发送者（群机器人等），按群号匹配时 7 天漏进大量消息，修复后 0 条；特别关心同理，填 QQ 号也能标 ★重点（控制台星标卡原本就写「群号或 QQ 号」，现在名实相符）
- 看板移植 DeskBox 体验四件套：①唤出淡入 + 尺寸变化 ease-out 缓动动画（`[ui] anim` 可关）；②Win10+ 毛玻璃 `[ui] blur`（SetWindowCompositionAttribute，本机实测 API 接受、无色键冲突/黑角，与半透明叠加呈亚克力效果，默认关）；③启动时按多显示器工作区钳位窗口位置（副屏拔掉后坐标不落虚空）；④边框细调 `[ui] border_width`（0-4）/ `[ui] border_color`；控制台外观卡新增「动画」「毛玻璃」开关；修复热重载后浅色主题残留、启动动画阶段窗口被顶到 (0,0) 的问题
- 看板再搬 DeskBox 窗口行为三件套：①拖左右边缘改宽度（280-560，拖右缘位置不动、拖左缘右边界不动，宽度随位置一起记忆，热重载不覆盖）；②拖动松手时距工作区边缘 8px 内自动贴边（`[ui] snap` 可关，控制台外观卡新增「边缘吸附」开关，支持多显示器各自边缘）；③背景右键菜单新增「锁定位置 / 锁定尺寸」开关（防误拖，状态持久化到 dash_pos.json）
- 看板继续对齐 DeskBox：①四角锚点定位（dash_pos.json 记锚点+边距，换分辨率/换屏后启动仍贴住原来的角落）；②唤出滑入动画（从最近屏幕边缘滑入 24px，与淡入同步约 210ms）；③唤出到期沉回桌面层前先淡出约 150ms；④迷你胶囊独立位置记忆（展开/收起各回各的上次位置）；⑤交互时临时抬层级（点一下看板浮到前面，15 秒无操作沉回）；⑥迷你胶囊悬停展开（停 0.4s 展开、离开 0.8s 自动收回，`[ui] hover_expand` 默认关，控制台外观卡新增开关，输入框打字时不收回）
- 看板交互调整：双击条目文字定位原文出处（单击不再弹窗，避免误触）；每条待办最右边新增「改」按钮（⋯ 菜单左移），点一下弹小窗改待办文字、回车即保存（Store 新增 `update_text`），右键菜单同步加「修改内容」
- 双击定位支持 QQ 下载文件：源头消息 / 待办文字里提到文件名（如 `[文件] 周报.docx`）时，直接在 QQ 下载目录（Tencent Files 含 NTQQ nt_data、系统下载目录）里找到并用资源管理器选中（多个同名取最新）；找不到再退回原文窗口；遍历放后台线程不卡界面
- 群号显示成群名：新增 `chat_names` 表（旧库自动建表），`db-import` 同步时顺带从 nt_msg.db 同级的 `group_info.db` 刷新群名（60001=群号 / 66530=消息里的内部代码 / 60007=群名，两个 ID 都入映射，失败静默不影响同步）；控制台「免打扰 / 特别关心」卡片的号码胶囊显示为「群名（号码）」，查不到名字的（如机器人 QQ 号）保持原样；`purge-chat --list` 新增群名列
- 测试 103 → 146
- 修复同步/监视/定时任务未传 `--db` 导致写入随工作目录漂移的 c2t.db（与看板读取的 qq.db 不一致）；截图待办默认库与控制台统一为 qq.db；c2t.toml 的 db_path 已指向 qq.db，历史残留 c2t.db 已清理
- README 按用户/程序分层口径重写（纯小白 → 开发者五层），与分层方案.md 对齐
- 控制台「立即同步」切换到内置引擎（`db-import`），日常同步不再需要 Docker
- 控制台「设置」的口令保存改走 `chat2todo.core.keybox`（DPAPI 直存）
- 入口脚本收敛为 2 个：`console.bat`（启动控制台）、`后台同步.bat`（计划任务），路径全部自解析、免安装
- 移除 Docker 导出链路（sync.py / export.py / pipeline.py / run.sh / review.py）及硬编码旧路径的重复 bat/vbs

## [0.4.1] - 2026-10-24

- 内置后端改为真实能力：新增 AES-256 与 SQLCipher 首页校验（core/crypto.py）
- `c2t db` 在有 key 时直接给出「密钥是否通过」结论
- 测试 36 → 41

## [0.4.0] - 2026-10-24

- 新增数据库只读通道（可选，默认关闭）：`c2t db` 检查库、`c2t db-import` 拉取消息
- 双后端：`community`（调用社区解密工具，读 JSONL）/ `builtin`（标准 SQLCipher 骨架）
- 自动探测真实「文档」目录（读注册表），修复 Documents 被重定向后找不到库
- 新增错误码 E4001-E4004，均带可执行建议
- 测试 30 → 36
# 更新日志

## [0.3.0] - 2026-10-24

P5：补齐 AI 接线、新增微信适配器、看板换悬浮样式。

### 新增

- 微信 txt 适配器 `wechat_txt`：兼容三种行头（首行正文、同行情冒号、方括号时间戳）
- AI「说话人意图」与「同事实归并」正式接入导入流程（此前只有方法没接线）
- 看板改为悬浮贴纸样式：圆角半透明、数字时钟、日期星期、彩色圆点、自适应高度
- 看板支持「＋ 添加」手动待办与「清空」

### 修复

- 归并后新描述的置信度固定降为 0.70，避免 AI 改写拉高原本的规则置信度

## [0.2.0] - 2026-10-24

P2 + P3 + P4：检索、双视图、导出、配置、AI 钩子、看板与打包。

### 新增

- 本地检索：FTS5 全文索引，2 字中文查询自动退回模糊匹配
- 双视图：`c2t show <id>` 查看某条消息的前后原文
- 三格式导出：`c2t export --format md|csv|json`
- 配置文件 `c2t.toml`，九个键 + `[ai]` 段；`c2t doctor --write-sample` 生成示例
- AI 五环节钩子（边界复核 / 同事实归并 / 说话人意图 / 待办改写 / 口吻改写），全部默认关闭
- 环境自检 `c2t doctor` 八项检查，错误码 E1001/E1002/E2001/E3001/E9001
- `c2t stats --since 7d` 时间窗统计；Obsidian 双链导出
- 桌面看板 `c2t dash`：置顶窗口、勾选即完成、定时刷新（tkinter，仍零第三方依赖）
- 单文件打包 `python tools/build_pyz.py` → `dist/c2t.pyz`

### 修复

- Windows 记事本保存的 TOML 带 BOM 时解析失败
- `--config` 只能写在子命令之前

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
