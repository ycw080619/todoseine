## [Unreleased] - 2026-10-30 · 多选批量操作 / 拖动排序 / 回收站30天过滤

### 新增

- **多选 + 批量操作**：页脚「多选」按钮进入批量模式，卡片正文点击切换选中态（主色 16% 透明叠加），
  批量栏提供「全选 / 完成 / 标重点 / 删除 / 取消」。删除弹确认框；标重点在 `reason` 里增删「★重点」
- **拖动排序**：卡片左键拖动超 8px 启动 `QDrag`（预览用卡片截图），落点行之前插入。
  `display_order` 列手动排序优先于自动排序（完成 / 重点 / 时间）；多选模式下禁用单条拖拽避免手势冲突
- **回收站 30 天过滤**：回收站页加「最近30天 / 全部」切换，`list_trash` / `count_trash` 支持 `days` 参数
- **回收站搜索**：回收站顶部加搜索框，大小写不敏感匹配内容 / 来源 / 发送人；搜索时计数显示「匹配 N 条」，无结果提示换关键词
- **设置面板加 QQ 库密钥配置**：「数据与日志」分组里新增密码输入框 + 保存按钮，DPAPI 加密存 `~/Chat2Todo/dbkey.dat`；空输入可清除已有密钥；状态行显示是否已保存及路径。小白用户不用再敲 `c2t db-key --save`

### 修复

- **拖拽落点偏后一位**：`pop` 源行后若源在目标之前，目标已左移一位，插入点需 `target_idx - 1`
- **数值框 / 下拉框滚轮误触**：`mousePressEvent` 置 `_wheel_active=True`、`focusOutEvent` 置 False，
  `wheelEvent` 检查标志位才响应（`hasFocus()` 不准因内部 QLineEdit 持焦，`focusWidget()` 不准因 widget 显示自动获焦）
- **吸边隐藏后重启点不出来**：`_restore_geometry` 改用「中心点在屏幕内」判断（`intersects` 在只剩 6px 时仍返回 True）；`_save_geometry` 在吸边时保存可见位置

### 测试

- `pytest` 419 passed / 11 subtests；`ruff` 全绿

## [Unreleased] - 2026-10-30 · 6 个精致交互特效

还原西瓜同学《让AI做的App和小程序更精致的6个交互》里可落地到看板的 4 个特效
（吸附卡+画线、惯性滚轮因看板无对应场景跳过；SmoothScrollArea 已是惯性滚动等价物）。
所有动效时长走 `theme.motion_ms()`，尊重 `anim_speed=off` / `anim_effect=none` / 系统「关闭动画」。

### 新增

- **完成反馈按钮**：勾选点按下时整体挤压缩小（对应「先收成圆」）→ 对勾分两段一笔笔画出
  （短臂 0–40%、长臂 40–100%）→ 从勾选点迸发 11 颗带重力的粒子（强调色 / 主色亮调 / 竖条色）。
  粒子画在卡片内部并裁剪到圆角路径，避开半透明分层窗口的合成问题；勾选后延迟 420ms 再重建列表，
  让动画播完不被 reload 打断
- **勾选联动**：页脚统计数字（待办 / 已完成 / 本周清 / 新增）勾选时平滑滚动插值，不再瞬间跳字；
  页脚左侧新增完成进度环（`done / (open+done)`），弧长随勾选联动填充
- **叠卡展开**：点「展开 N 条」时，首屏之外的卡片以 30ms 间隔 stagger 生长（`maximumHeight`
  `OutCubic` 0→62）；收起时倒序缩回，全部缩完再重建列表。动画期间行若被 reload 销毁则容错跳过
- **圆形扩散转场**：页签切换时，从被点击的页签按钮中心扩散出一个圆（`DestinationOut`
  在旧页截图上挖洞），逐步揭开新页；`anim_effect=none` 时直接落位

### 测试

- `pytest` 419 passed / 11 subtests；`ruff` 全绿
- 临时脚本逐帧截图复核：勾选对勾+粒子、页脚进度环、展开 stagger、圆形扩散转场

## [Unreleased] - 2026-10-30 · 全窗口渲染 / UX 巡检

对悬浮看板、回收站、设置（7 页签）、统计、关于逐一做渲染与交互复查，修掉的都是实测确认的问题。

### 修复

- **设置页撑出屏幕**：外观页自然高度 1300px 以上，塞进 QTabWidget 会把窗口顶到 2030px 物理高、底部「保存配置」跑到屏幕外。新增 `_wrap_scroll()`，7 个页签全部套透明 QScrollArea
- **滚动条看不见**：8px 暗色条几乎不可见，用户不知道页签内还有内容。加宽到 10px 并提亮把手（`hover` 再提亮），设置页与 `pages.py` 的统计 / 关于 / 回收站共用同一套样式
- **基础页预览框被拉高**：`stretch=1` 把「实时消息预览」框撑到 927px（自然高度仅 229），改为 `setFixedHeight(150)` + 末尾 `addStretch(1)`
- **回收站行高忽高忽低**（72px / 158px，第 7 行文字被推到底部）：`ElidedLabel` 只横向省略、没锁单行，含换行的文本让 QLabel 按多行绘制并撑高 `sizeHint`。改为 `sizeHint` / `minimumSizeHint` 强制单行高度，`paintEvent` 把换行压成空格并强制 `TextSingleLine`
- **置顶开关是死开关**：`always_topmost` 只有设置页在读写，`DEFAULTS` 里没有这项、悬浮窗硬编码 `WindowStaysOnTopHint`。补默认值 + `set_topmost()` + `apply_config()` 接线
- **`set_topmost` 一关窗口就消失**：`setWindowFlag` 会先隐藏窗口重建句柄，改完再读 `isVisible()` 已为 False，`show()` 永不执行。改为改前捕获 `was_visible`
- **设置 / 统计 / 关于窗口被悬浮窗压住**：悬浮窗常驻置顶，子窗口 `raise_()` 越不过去。打开前先把悬浮窗降级，子窗口 `closed` 信号触发后按配置恢复
- **统计 / 关于窗口没有入口**：从悬浮窗拆成独立窗口后，`InfoWindow` 在代码里没有任何可打开路径。托盘菜单补「统计」「关于」两项，并接上同样的置顶降级逻辑；同时删掉未被任何地方 emit 的 `info_requested` 死信号
- **关于页热键写死且写错**：提示里硬编码「Ctrl+Alt+D」，实际配置是 `ctrl+shift+d`。改为从 `hotkey.resolve_specs()` 读当前配置，格式化成 `Ctrl + Shift + D`
- **统计总览加不起来**：判为噪音的 498 条算进「待办总数」却没有对应行，各项相加永远对不上。补「已忽略」行
- **独立窗口依赖系统主题**：程序只有暗色一套配色，但两个独立窗口没设底色，系统切浅色主题时会出现白底浅字。显式指定 `theme.PANEL` 底色，并给信息窗口的 QTabWidget 补上与设置页一致的页签样式
- **锁定按钮是彩色 emoji**：`🔓` 走 Segoe UI Emoji 渲染成彩色字形，和旁边 ⊟ / — / □ / × 不一致。改用 Segoe MDL2 Assets 的单色锁形（`E72E` / `E785`）
- **看板搜索框分隔线像括号**：1px 竖线紧贴输入卡圆角，视觉上像一个「[」。改成 `margin-left` 留出空隙

### 测试

- `pytest` 419 passed / 11 subtests；`ruff` 全绿
- 全窗口截图复核：看板（460 / 420 宽）、迷你胶囊、回收站、统计、关于、设置 7 页（顶部 + 滚到底）

## [Unreleased] - 2026-10-30 · 设置项扩充（对齐 DeskBox 外观口径）

把 DeskBox 里能真实生效的外观设置搬进 TodoSeine，不做占位开关。

### 新增

- **系统材质下拉**：`dash.material` 从布尔改为字符串，6 档 `off / acrylic / mica / mica_alt / tabbed / blur`，`_apply_material()` 按值分发到 DWM 对应 API；旧配置的 `true/false` 由 `_material_from_raw()` 自动兼容
- **材质浓淡** `material_intensity`：控制亚克力 tint 的 alpha（10%–100%），越浓越不透
- **动画速度** `anim_speed`：`off / fast / normal / slow` 对应 0 / 0.6 / 1 / 1.5 倍时长，落到 `theme.set_motion_scale()`
- **动画效果** `anim_effect`：`slide`（滑动）/ `fade`（淡入淡出，走窗口级 `windowOpacity`，不用离屏合成）/ `none`（直接落位）
- **系统圆角** `corner_round`：DWM 圆角偏好 `ROUND` / `DONOTROUND`
- **边框跟随强调色** `border_accent`：中性灰 ↔ 主色 `#6FA893`
- **吸附距离** `snap_gap`：2–60 px，替代写死的 16 px
- **页脚统计** `footer_stats`：可隐藏「待办 x · 已完成 y · 本周 z」
- **清除已完成按钮** `clear_done_button`：可隐藏页脚按钮
- **回车行为** `todo_enter`：`enter` 直接添加 / `ctrl_enter` 需 Ctrl+回车（在 `eventFilter` 里拦截 KeyPress，因为 `returnPressed` 拿不到修饰键）

### 修复

- **下拉箭头不可见**：Qt 的 QSS 在 `::down-arrow` 上不保证渲染 border 三角，系统默认箭头又是深色。改为 `_Combo(QComboBox)` 子类在 `paintEvent` 里自绘 8×4.5 的浅色三角，设置页 9 个下拉全部替换

### 测试

- `pytest` 419 passed / 11 subtests；`ruff` 全绿
- `test_qt_theme.py` 新增 3 条动画速度档位断言（off 归零 / fast 0.6 / 未知回退 normal）

## [Unreleased] - 2026-10-30 · 界面微明度体系落地

把 `docs/ui/设计规范-组件与交互.md` 的令牌与交互口径接进看板和设置页。

### 新增

- **`qt/widgets/theme.py` 重写为令牌表**：表面阶梯（面板 `#171A1E` → 卡片 `#1C2025` → hover `#23272D`，相邻明度差 2.4% / 3.0%）、细线（白 7% / 12%）替代硬边框、文字三级（对比度 14.3 / 7.9 / 4.5:1）、字重降档（正文 400、标题 550→Qt 落到 DemiBold）、`mix()` 插值、`css()` 输出、`tabular()` 等宽数字
- **`qt/widgets/focus_ring.py`**：输入框外环柔光（`QGraphicsDropShadowEffect`，offset 0 / blur 10 / 30% 主色），聚焦进出自动开关；内环仍是 QSS 恒 1px 边框只换颜色，所以聚焦零尺寸变化
- **看板与设置页接入**：两个输入框、设置页全部 `QLineEdit` / `QSpinBox` 统一细线 + 聚焦换色 + 柔光；标题改 `title_font`、说明改为等宽数字字体、时钟去掉写死的 Consolas 内联样式、统计大数字走 `tabular()`
- **`docs/ui/design-system.html`**：单文件参考实现（明暗切换、动效开关、可拖拽列表、打字动画重播、计时器），带 `?selftest=1` 页内断言与 `?only=N` 分面板截图开关；配套 8 张实拍截图存 `docs/ui/shots/`
- **`docs/ui/设计规范-组件与交互.md`**：令牌表、组件状态表、CSS/JS 实现要点、14 条可验收标准、Qt 映射与实测 JSON
- **系统级「减少动态」接入**：`theme.reduced_motion()` 用 `SystemParametersInfo(SPI_GETANIMATION)` 读 `iMinAnimate`（即 Windows「辅助功能 → 视觉效果 → 动画效果」开关），`theme.motion_ms()` 据此把时长归零。已接到窗口滑入/缩放（`main_window.py`）、Toast 淡入淡出、卡片 hover、页签滑块、看板滚动动画——关掉动画时全部直接落位，不再有位移与模糊
- **设置页下拉框 / 勾选框令牌化**：`_COMBO_STYLE` / `_CHECK_STYLE` 统一 1px 细线、展开只换色（宽度不变）、弹层底色 `CARD_BG`、选中底 `ACCENT_SOFT`、勾选点 14px 自绘方点填主色，不再有系统白底控件

### 修复

- **hover 变成"绿描边"**：早期实现给卡片边框掺 18% 主色，实测 alpha 从 7% 翻到 ~24%，视觉上是一条彩色硬边。改为只把 alpha 提到 12%、RGB 不动
- **暗色字重过粗**：`pages.py` 的标题用 `setBold(True)`（700），暗底上会外溢光晕；改为令牌字重（Qt DemiBold 600 封顶）
- **令牌测试误报**：`setFeature(tnum)` 只是记录标志、不代表字体真的支持，改成用 `QFontMetrics` 实测 `1` 与 `8` 的步进宽度相等
- **下拉框没有下拉箭头**：给 `QComboBox::drop-down` 写了样式后 Qt 不再画默认箭头，控件看起来"点不开"。删掉该子控件规则，截图对比确认箭头恢复
- **诊断信息**：重复启动时日志不再只写「已有实例在运行」，而是带上父进程 `pid:名称`（`tray.launcher_brief()`，ToolHelp32 纯 ctypes），下次再出现能直接看出是谁拉起的

### 测试

- `pytest` 415 passed / 11 subtests；`ruff` 全绿
- 新增 `tests/test_qt_theme.py`（13 条：WCAG 对比度、明度差上限、字重降档、数字等宽、细线透明度、颜色插值边界、动效降级、下拉/勾选样式口径）
- 新增 `tests/test_qt_focus_ring.py`（6 条：聚焦环开关、零尺寸变化、柔光半透明且居中、hover 不许变彩色描边）
- 离屏渲染实测：看板与设置页在真实 Windows 平台渲染正常（offscreen 平台缺中文字形，会渲染成豆腐块，仅用于跑几何断言）

## [Unreleased] - 2026-10-30 · 补齐开发定稿剩余项

对照《QQ消息智能处理系统 完整全流程（开发定稿版）》逐项核对后补齐 3 项功能 + 3 份文档。

### 新增

- **半自动转发**（`core/forward.py` + `qt/forward_dialog.py`）：默认关闭，看板右键「转发…」才可见。预览窗 → 3 秒倒计时（发送键置灰）→ 手动确认 → 关闭窗口后模拟按键输入（`SendInput` Unicode 码元，支持 emoji）→ 审计留痕。倒计时期间切换窗口自动取消；不提供批量 / 定时 / 自动重试；可勾选 AI 口吻改写（失败自动回退原文）。审计写 `~/.chat2todo/forward_audit.jsonl`，**只记目标 / 内容哈希 / 长度 / 结果，不落原文**
- **截图归档**（`core/screenshot_archive.py`）：设置面板早有开关但一直没接线，现在真正落盘到 `archive/screenshots/`，按文件名时间戳判定保留期，启动时自动清理超期图；默认关闭时截图仍只在内存里过 OCR，不写磁盘
- **看板优先级排序**：`★重点` 条目置顶（文档「按优先级从高到低」），已完成条目仍沉底

### 文档

- `docs/Win11-兼容性验收清单.md`：材质三级降级链（Mica Alt → 亚克力 → 纯色）、吸附 / 多显示器 / 缩放 / 焦点 / 截屏隐藏 / 采集通道 / 通知 / 卸载的逐项验证方法与期望结果
- `docs/终态版-vs-DSH版-功能对比矩阵.md`：19 个维度逐项对比 + 老用户迁移映射
- `SECURITY.md`：补「未签名运行提示」（SmartScreen / Defender 误报说明、SHA256 校验、源码运行替代）与「误报提交指引」（四步流程）

### 测试

- `pytest` 394 passed / 11 subtests；`ruff` 全绿
- 新增 `tests/test_forward.py`（7 条：默认关闭、内容规整、哈希稳定、审计不含原文、审计分页、空内容不注入）
- 新增 `tests/test_screenshot_archive.py`（4 条：默认不落盘、独立目录、只删超期、关闭时 no-op）
- 新增 1 条看板排序回归用例（★重点置顶）

## [Unreleased] - 2026-10-29 · PySide6 版看板（Qt 里程碑）

Tk 版看板（`dashboard/`）不动，新增可选的 `todoseine.qt` 层：托盘常驻、玻璃质感看板、实时监听。

### 新增

- **实时监听管线**（`qt/monitor.py`）：QThread 定时抓 QQ 窗口 → 截屏 → Win11 OCR → 抽取 → 入库 → 通知看板。失败指数退避（最长 60s），支持暂停/恢复/立即同步，配置改了热生效（不重启线程）
- **只读截图通道重写**（`sources/uia.py`）：放弃模糊匹配的 uiautomation，改用 `EnumWindows` 精确匹配（标题=QQ 且类名=Chrome_WidgetWin_1）+ `PrintWindow(PW_RENDERFULLCONTENT)`，QQ 在后台也能抓；全黑判定失败后回落前台抓屏
- **裁剪自适应**：会话列表宽度可拖动，`_detect_chat_left()` 找「列主色第一次变成聊天区底色」的位置当分界；`_detect_chat_right()` 再从右往左找回来，群成员/公告面板展开时也只剩聊天区；聊天区底色取「列主色众数」，正中正好是气泡也不会认错
- **托盘**（`qt/tray_icon.py`）：QPainter 现画多尺寸图标，菜单含显示主窗口/立即同步/暂停监听/开机自启/打开数据目录/退出；窗口可见时不弹气泡，交给看板 Toast
- **通知三通道**：新待办托盘气泡、看板 Toast（可点「查看」跳转）、到期提醒（`due_remindable` + 60s 扫描，同一批不重复提醒）
- **看板交互**（`qt/main_window.py` / `dashboard.py`）：位置记忆（600ms 防抖）、贴边吸附、拖左右边改宽、迷你胶囊、锁定、右键（判定理由 / 此类不再提醒 / 定位下载文件 / 移入回收站）、撤销
- **截图建待办**（`qt/snap.py`）：`Ctrl+Shift+S` 全屏遮罩拖框 → OCR → 可编辑确认框 → 入库
- **五页看板**：待办 / 回收站（还原·彻底删除·清空）/ 统计（总览·近 7 天·判定校准）/ 设置（基础·监听·AI·外观四页签，写回 TOML）/ 关于
- **设置落盘**：`config.save()` 手写 TOML（不引入 tomli-w，守住核心零依赖），新增 `[uia]` `[dash]` `[hotkey]` `[notify]` 四段默认值
- **打包**：`todoseine_qt.spec` + `tools/qt_launcher.py` 入口 + DLL/插件白名单，产物 103.4 MB → 51.4 MB，`console=False` + 图标 + 版本资源

### 修复

- OCR 汉字间空格：`normalize_cjk_spaces()` 追加「全角标点两侧空格一律删」，「19 ： 04」不再带着空格入库（「明天 10:30」仍保留空格）
- 右键「定位下载文件」改为后台线程扫下载目录，不再冻界面几秒；同时只允许一个扫描在跑
- 静音命中已有规则时不给「撤销」，避免误删用户以前设的规则
- 入库消息 `session` 为空的问题：`Monitor._run_once` 补传 `session`
- PyInstaller 以 `__main__` 执行 `qt/main.py` 导致相对导入失败、双击即退：入口改为 `tools/qt_launcher.py`

### 测试

- `tests/test_qt_smoke.py` 20 条：热键解析、图标生成、批抽取、设置回填、离屏渲染五页、回收站往返、还原保留状态、截图入库、裁剪三边界、后台定位、监听启停幂等、配置往返
- `pytest` 303 passed / 1 skipped / 11 subtests；`ruff` 全绿

## [Unreleased] - 2026-10-29

看板「DeskBox 式系统材质」上线（默认关，不影响现有行为）。

### 新增

- **`[ui] material = "acrylic" | "mica" | "off"`**：看板可选系统材质——DWM 原生圆角（替代 canvas 手绘圆角的锯齿）+ Acrylic 亚克力磨砂背景（`mica` 在系统材质不渲染时自动垫 Acrylic 兜底）。纯 ctypes 直调 DWM，零第三方依赖，与「纯标准库」约束不冲突
  - 配方对齐开源库 pywinstyles 并实测验证：窗口背景涂纯黑 + 未公开属性 30/AccentState 3 + `DwmExtendFrameIntoClientArea(-1)`
  - 两条实测出来的互斥铁律（违反则材质静默失效，窗口变纯黑/全透明）：
    1. 不能用 `-transparentcolor` 颜色键
    2. 不能用 `-alpha`（会挂 WS_EX_LAYERED）——材质模式下整窗透明度全部经 `_win_alpha()` 守卫变为空操作，不透明度改由 Acrylic 底色承担；唤出淡入/沉底淡出动画在材质模式下自动退化为直接显隐
  - 改尺寸会重置 DWM 玻璃帧扩展 → `_set_size` 每次落地后回刷材质
  - 修复连带遗留：早报空态卡片一处星期索引仍是 `日一二三四五六`（tm_wday 周一=0，会错一天），统一为 `一二三四五六日`

### 测试

- 新增 `TestMaterialConfig` 5 条：默认 off、acrylic 强制 alpha=1.0、mica 三态、非法值回退、材质模式 `_win_alpha` 空操作
- 实测验证：acrylic 模式与 pywinstyles 对照窗口逐像素一致（(84,84,84) 磨砂表面）；mica 模式在本机回退 acrylic 正常；默认 off 模式面板色逐像素回归（#212024 分毫不差）
- `pytest` 288 passed, 11 subtests passed

## [Unreleased] - 2026-10-28

针对一次逐行代码审计出的 11 条硬伤，本次修掉其中 9 条（另 2 条记录为待办技术债）。

### 安全（会真出事的两条）

- **CORS 校验绕过**：`api.py` 的 `_is_local` 原来用 `addr.startswith("127.")`，`Origin: http://127.0.0.1.evil.com` 会被当成回环地址放行 ACAO，任意网页能读走本地待办数据。改为 `ipaddress.ip_address(addr).is_loopback` + 精确匹配 `localhost/::1`，域名一律拒绝
- **SQLite 外键形同虚设**：`Store.__init__` 现在每条连接执行 `PRAGMA foreign_keys = ON`；`feedback.todo_id` 改为可空并在删待办（`trash_item` / `trash_many` / `purge_chat`）后由 `_heal_feedback_orphans()` 解绑为 NULL，不再留孤儿校准样本
  - 迁移：老库 `todo_id NOT NULL` 会被 `_rebuild_feedback_nullable()` 自动重建（实测真实库 0 样本无痛升级）
  - 不用 FK 的取舍：校准档案要跨机器导入，落地时源库 todo_id 在本库不存在，硬约束会让整份档案导入失败；等价语义用显式 SET NULL 实现
- **手写 AES 收边**：删掉 `core/crypto.py` 里没人用的 `find_passphrase`，模块文档补上「仅服务口令校验、不得用于整库解密」的边界说明。纯 Python AES 保留是因为 `c2t db-key --save` 要在零第三方依赖下校验口令

### 健壮性

- **FTS 增量同步**：`_sync_fts` 从 `NOT IN` 全表 diff 改为 watermark（`meta.fts_last_id`），只补水位之后的新消息；按群清理后走 `_rebuild_fts()` 整表重建。真实库（51966 条）二次打开不重复灌入
- **极简 TOML 解析器支持数组**：`ignore_groups = []` / `star_groups = ["111", "222"]` 现在能正确解析成列表（示例配置里本来就有数组，之前会存成字符串或报错）
- **默认库路径与文档对齐**：`config.DEFAULTS["db_path"]` 从相对路径 `c2t.db` 改为 `%USERPROFILE%\Chat2Todo\qq.db`（与控制台/看板同库）；配置项留空即回落默认值，`Store` 自动建父目录。实测在任意 cwd 跑 `c2t stats` 都读同一个库、不会凭空多出空库
- **异常不再全静默**：`cli.main` 增加 `logging.basicConfig`（默认 WARNING，`C2T_DEBUG=1` 放开 DEBUG）；注册表探测、群名读取、校准权重加载等容错分支改为记 debug 日志并说明后果，FTS5 不可用改为 warning

### 文档

- `项目说明.md` / `分层方案.md`：测试数量从过期的 112 / 103 更正为 202；插件机制状态从「未开放」更正为「已开放（`c2t plugins` + `plugin_manifest.py`）」

### 测试

- 新增 4 条回归：`test_is_local_rejects_prefix_lookalike_hosts`（CORS 伪装域名）、`test_fts_incremental_watermark`、`test_feedback_orphans_not_left_behind`、`test_config_parses_inline_arrays`
- 真实库副本冒烟：`PRAGMA foreign_keys=ON` 升级、FTS 水位初始化（51966 条不重灌）、删待办解绑全部通过
- `pytest` 202 passed；`ruff check .` All checks passed；`c2t serve` 实测：`127.0.0.1.evil.com` / `localhost.evil.com` 均无 ACAO，`localhost:5173` 正常放行

### 待办（本次未动）

- `Store` 873 行职责过多，建议按 messages / todos / fts / feedback 拆成多个 mixin 或仓储类
- `api.py` 每请求新建 `Store`：本地工具并发极低，暂不引入连接池（`ThreadingHTTPServer` 每连接一线程，线程本地缓存反而会漏连接）

### 第二轮代码级复查（C1–C6）

逐条对着代码读出来的问题，按严重度：

- **C1 🔴 SQLite 无并发保护**：看板每 30s 新建连接、控制台同步线程写库、计划任务 2 小时一次 db-import，三路并发打同一个库，没有 WAL + busy_timeout 会随机抛 `database is locked`，又被上层 `except: pass` 吞掉，用户只看到「看板不刷新」「同步没结果」。每条连接现在设 `journal_mode=WAL` / `busy_timeout=5000` / `synchronous=NORMAL`，`sqlite3.connect` 也带 `timeout=5`
- **C2 🔴 `todo_threshold` 是死配置**：`config.py` 定义且 `sample_toml()` 显著标注，但 `extract.py` 硬编码 `TODO_THRESHOLD=4`，用户改 `c2t.toml` 完全不生效。`extract_todos` 新增 `todo_threshold` / `weak_threshold` 形参，CLI 三个调用点和看板 `explain_todo` 都从配置传值；`pending_threshold` 同样接通
- **C3 🟡 OCR 永久改超时**：`ai.py` 把 `self.timeout = max(self.timeout, 120)` 改成实例属性，调过一次 OCR 后后续所有 AI 调用都被悄悄拉到 120s。改为 `_chat` 接收 `timeout` 参数，OCR 单次传 `max(self.timeout, 120)`
- **C4 🟡 静默失败**：src/ 里唯一的 broad `except Exception: pass`（看板早报状态更新）改为 `logger.debug`，其余是特定异常类型的有意降级
- **C5 🟡 每次 Store 都跑全表迁移**：`_migrate` 里 `UPDATE feedback ... NOT IN (SELECT id FROM todos)` 是全表子查询，看板每 30s 跑一次会卡。新增 `SCHEMA_VERSION` 水位，`meta.schema_version` 命中就直接 return
- **C6 🟡 `verify_sqlcipher_key` 默认迭代数错**：`derive_key` / `verify_sqlcipher_key` 默认 `iterations=256000`（SQLCipher 4 默认），但 QQ 实际是 4000，按默认值调用必 100% 失败。默认值改为 4000 并在文档字符串注明

测试：新增 3 条回归（阈值生效、WAL+busy_timeout、迁移只跑一次），pytest 205 passed、ruff clean；真实库副本迁移通过（WAL、busy_timeout=5000、schema_version=3、0 孤儿、二次打开跳过迁移）

## 2026-10-27

### 工程化：控制台分页拆分与 UI 回归测试

- `tools/qq_db_export/console.py` 从 1692 行降到 1318 行：六个页面（控制台 / 常规 / AI / 截图 / 最近删除 / 关于）搬进新包 `tools/qq_db_export/c2t_pages/`，每页只暴露 `build(app, ctx)`
- 页面模块不 import console（避免循环导入），所有配置读写与路径通过 `ctx` 注入；`_read_cfg` 等函数仍在 console 模块取值，测试替换 `CONFIG / ROOT` 依旧生效
- `src/todoseine/dashboard/app.py`：右键菜单填充抽成模块级 `_fill_item_menu(menu, item, *, mark, edit_item, show_detail, explain_why)`，与 Tk 解耦
- 新增 `tests/test_dashboard.py::TestItemMenu`（3 条）：用假 menu 断言菜单项集合、完成/恢复分支、三个动作回调的路由，补上「测试全绿但右键必崩」的盲区

### 看板横向滚动条：长文本不再截断

- `src/todoseine/dashboard/app.py`：待办条目正文不再截断（`body = item.text`，保留 ★ 前缀），超长文本由底部横向滚动条拖动查看
- 新增 `HBAR_H = 14` 常量，`_set_size` 中画布高度减 `HBAR_H` 给滚动条留位
- `_on_hscroll(*args)` 处理 `moveto` / `scroll`，更新 `state["x_off"]` 并 pop `render_hash` 强制重画
- `_render_full` 中所有待办条目元素 x 坐标加横向偏移 `xo`；循环后按最宽条目溢出量计算 `x_max`，仅 `x_max > 0` 时 `hbar.pack()`，迷你/每日模式 `pack_forget()`
- 修复滚动方向：`xo = -x_off`（x_off>0 表示滚到右侧，内容整体左移，露出长文本后半段）
- 修复 `NameError: name 'state' is not defined`（`_hbar` 移入 state 字典字面量）与 `UnboundLocalError: err`（缓存命中路径提前 `err = ""`）
- **修复横向滚动时短文本变空白**：所有条目共享全局 `x_off`，长条目滚到右侧时短条目被完全滚出可视区；改为按条目自身溢出量 `item_overflow = max(0, body_w - text_area_w)` 限制偏移 `xo = -min(x_off, item_overflow)`，短条目只滚到自身溢出量，始终可见
- **修复拖动左右边缘改宽度时文本不重渲染**：render debounce 元组不含 `w`，拖拽后 `render()` 被跳过；debounce 加入 `state.get("w", W)` 与 `state.get("x_off", 0)`，且 `_apply_width` 末尾调用 `render()` 实现拖拽中实时重排，文本不再截断/错位
- **新增顶部折叠堆叠提示**：原来只有列表底部显示「下方还有 N 条」的堆叠卡片边；现在向上滚过之后（`offset > 0`）顶部同样露出窄胶囊「↑ 上方还有 N 条 · 滚轮往上翻」，压在首张卡片上沿，与底部提示对称
- **新增滚轮滚动动画**：`_slide_scroll(dirn)` 在每次滚轮后把本屏条目（`rows` 标签）整体沿滚动方向先位移 12px，再用 9 帧 `_ease_out_cubic` 缓动在约 140ms 内收敛回原位；`slide_gen` 世代号在重画/新滚动时打断上一段动画，避免叠加抖动

### 本地 HTTP API：待办数据对外可用（B1）

- 新增 `src/todoseine/api.py`（纯标准库 `http.server`，零第三方依赖）：5 个端点
  - `GET /health` 健康检查
  - `GET /todos?status=&limit=` 待办列表（pending / done / noise / 空=全部）
  - `GET /stats` 统计摘要 + 周摘要
  - `GET /search?q=&limit=` 全文搜索消息
  - `POST /todos/{id}/done` 标记完成（请求体可选 `{"status": "pending"|"done"|"noise"}`）
- CLI 新增 `c2t serve [--host 127.0.0.1] [--port 8765] [--verbose]`
- 安全边界（本地桌面工具，非公共服务）：
  - 只绑 `127.0.0.1`，且每个请求二次校验对端地址，非本地直连返回 403
  - 请求体上限 64 KB，只接受 JSON 对象
  - CORS 只回本机来源（`localhost` / `127.0.0.1`），不回 `*`，避免任意网页读取本地数据
- 每个请求各开一条 sqlite 连接，规避 `ThreadingHTTPServer` 跨线程共享连接的问题
- `tests/test_api_server.py` 14 用例：5 个端点的正常/错误路径 + 本机判定 + CORS 白名单
- pytest 194 passed（新增 14）；实测 `c2t serve` + curl 全端点通过

### ChatLab Format 深度对齐：双向交换

- 此前只有 `read`（导入 ChatLab），没有 `write`（导出）——适配器协议是单向的
- `adapters/base.py`：加可选 `write(path, messages, **kwargs)` 抽象方法，默认 raise NotImplementedError
- `adapters/chatlab_json.py`：实现 `write()`，按后缀输出 JSON 或 JSONL：
  - JSON: `{"schema": "chatlab-format-v1", "members": [...], "messages": [...]}`
  - JSONL: 每行一个对象，`_type` 区分 `"member"` / `"message"`（与 read 端严格对称）
  - members 自动从消息的 sender_id/sender_name 去重收集；可选 `chat_id` 参数保留群归属
- CLI 新增 `c2t export-chatlab <path>`：`Store.all_messages()` → `ChatLabJsonAdapter.write()`
- launcher `_KNOWN_SUBCOMMANDS` 加 `export-chatlab`
- tests/test_adapters_chatlab.py 6 用例：JSON/JSONL roundtrip 消息数一致 + members 去重 + schema 字段 + qq_txt/wechat_txt 无 write 抛 NotImplementedError
- pytest 180 passed（新增 6）；实测从真实库导出 51717 条消息 + 72 成员

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

### 判定理由报告：规则分解全链路可见

- `Store.explain_todo(todo_id)`：完整判定报告——规则 tag 分号拆分 + `extract.tag_meta()` 查规则描述与基础分 + 每条 tag 是否命中本组用户校准权重 + 原消息 + 前后消息上下文
- `extract.py` 新增 `CONFIDENCE_DENOM=7.0` 归一化分母 + `_TAG_META` 查 tag → (描述, 基础分) + `tag_meta()` helper
- CLI 新增 `c2t explain <todo_id>`：框线头部 + 原消息 + 规则分解 + 校准命中 + 上下文
- dashboard 右键菜单「判定理由」：Toplevel 弹窗复用 explain_todo 报告，show_detail 风格
- 实测 score_message L143 权重匹配逻辑（`if key and key in tag`）在 explain_todo 里镜像复现，确保报告与实际评分走同一套逻辑
- **dashboard TodoItem 字段修正**：`models.TodoItem` 只有 `todo_id` 没有 `id`，explain_why 写的 `item.id` 会 AttributeError——改为 `item.todo_id`（整 dashboard 统一用 todo_id）

### 插件注册协议（chat2todo-plugin-v1）

- 新增 `src/todoseine/plugin_manifest.py`：
  - Manifest dataclass：schema / id / name / version / permission_level / entry / capabilities / triggers
  - `validate_manifest(path_or_dict)` 严格校验：schema 不匹配拒、id 非 snake_case 拒、permission_level 非 file/db/uia 拒、必填缺失拒
  - **刻意不做文件系统检查**（manifest 可跨机器分发），只校验数据正确性
- `tools/qq_db_export/plugin.manifest.json` + `tools/snap_todo/plugin.manifest.json`：两个现有插件的真实 manifest 实例
- **CLI 新增 `c2t plugins`**：扫描 tools/*/plugin.manifest.json，校验后打表（id / 名称 / 版本 / 权限 / 能力 / 位置），让 manifest 从"死文件"变成可演示能力
- build.ps1 加 `--add-data <repo>/tools;tools` 把 tools 目录一起打进 exe（manifest 跟着走）；cmd_plugins 区分开发态（`src/todoseine → tools`）和 frozen 态（`sys._MEIPASS/tools`）
- tests/test_plugin_manifest.py 8 用例；pytest 174 passed

- 新增 PyInstaller onefile windowed 打包：`tools/todoseine-launcher.py` 单一入口同时路由控制台 GUI / CLI 子命令 / `-m todoseine` 子进程调用 / 独立 `.py` 脚本（frozen 跳过、开发态 exec），**console.py 源码零改动**
- 新增 `tools/build.ps1`：Set-Location + Join-Path 绝对路径拼接（cwd 无关），try/finally 恢复调用者 cwd；pip 分拆显式检查 `$LASTEXITCODE`，解决 `$ErrorActionPreference=Stop` 把 stderr 日志（pip warning / PyInstaller INFO）误判为 NativeCommandError 的问题；实测一键产出 17.87 MB onefile windowed exe
- `.gitignore` 加 `*.spec`（PyInstaller spec 文件机器特定，不入库）
- 四个 .bat（后台同步 / dash_autostart / console / launch）路径自解析重写：**不再硬编码** `C:\Program Files\Python312\python.exe`，也不依赖 `py -3w`（py launcher 在某些机器 exit 103 找不到 pythonw runtime）；改为 `where pythonw` 拿绝对路径、找不到 fallback `where python`；Windows 计划任务和开机自启跨 Python 版本都能跑
- **bat 启动崩溃修复（CRLF + UTF-8 BOM）**：全部 7 个 .bat 原是 LF 换行，cmd.exe 解析 .bat 必须 CRLF，导致双击弹出一连串 `'数据文件' 不是内部或外部命令` 后退出；统一转 CRLF + 新增 `.gitattributes` 锁定 `*.bat text eol=crlf`；`snap.bat` / `截图热键.bat` / `snap_autostart.bat` 的 `pyw -3` 改 `where pythonw` 自解析；`snap_autostart.bat` 去掉硬编码 `D:\QQ 待办项目` / `C:\Users\ycw`，改用 `%~dp0` + `%USERPROFILE%`；**补 UTF-8 BOM**：cmd.exe 默认用 GBK 读取 bat，中文 UTF-8 字节被误读后含 `&`/`>`/`^` 等特殊字符导致命令被错误拆分，加 BOM 后 cmd 从文件头识别 UTF-8 解析；实测全部 7 个 bat 正常拉起
- **控制台页面加横向滚动条**：`_new_page` 的 canvas 原只有垂直滚动条，「更多」等多按钮行在窄窗口下被水平裁剪且无法滚动；新增横向 Scrollbar + Shift+滚轮横向滚动，所有页面共用
- **修复控制台横向滚动条拖不动**：`canvas.bind("<Configure>", lambda e: itemconfigure(win, width=e.width))` 把 inner frame 宽度强制等于画布宽度，内容永远不溢出，横向滚动条永不激活；改为 `width = max(e.width, inner.winfo_reqwidth())`——内容窄时撑满画布（保留 fill=x 布局），内容宽时保留自然宽度 → 横向滚动条可拖动
- CI 收敛：Windows 11 × Python 3.12/3.13 + Ubuntu 冒烟；**加 pytest step**（原只跑 unittest discover 漏 36 个 fixture 测试），现 pytest 164 / unittest 128 全绿

### 看板每日早报三件套

- **每日早报 daily_mode**：每天首次启动自动切到卡片式摘要视图，`last_shown_date` 持久化到 `dash_pos.json`，当天不再重复切
- **每日 top-5 上限**：看板每日最多渲染 5 条待办（按 confidence 降序），剩余收在「展开」区，避免长列表淹没重点
- **周进度 footer**：看板底部新增 `· 本周清 N · 新增 M`，数据源 `Store.weekly_stats()` 纯查询方法，控制台「常规」页每周摘要卡复用同一函数

### 看板首次使用引导（用户体验 P0）

- **看板空状态按场景区分文案**：第一次打开（db 不存在）→ 👋「第一次使用 TodoSeine → Ctrl+Shift+C 打开控制台」；db 有但 todos 为 0（没跑 sync）→ 「点 ⚙ 打开控制台立即同步，5 秒自动刷新」；有 todos 但当前筛选没匹配 → 通用引导。不再一律显示「暂无待办」让用户懵
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
- 看板单文件拆包：`dashboard.py`（约 1800 行）拆为 `todoseine/dashboard/` 包——`app.py`（窗口与渲染）、`geom.py`（多显示器/吸附/锚点纯函数）、`win32.py`（单实例/热键/层级/毛玻璃）、`files.py`（文件名识别与定位）、`data.py`（数据层）；`__init__.py` 原样再导出全部名字，外部 import 路径与行为不变
- 群名映射自动补抓：控制台启动时若 `chat_names` 表为空，后台线程自动从 `group_info.db` 抓群名写库，完成后「免打扰 / 特别关心」两张卡片的号码即时换成群名显示（失败静默，不影响其他功能）
- `purge-chat --list` 的群名列改按显示宽度对齐（全角字符按 2 算），含中文群名时表列不再错位
- 控制台配置读取统一走 `todoseine.config`（`_read` / `load` 显式传路径），删掉两处手写 TOML 解析；顺带修复快捷键与 `[snap] ai_refine` 读取未传配置路径、实际总拿默认值的问题
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
- 控制台「设置」的口令保存改走 `todoseine.core.keybox`（DPAPI 直存）
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
