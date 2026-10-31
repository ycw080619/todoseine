# 分发与打包

TodoSeine 的核心承诺是**零第三方依赖**，因此分发方式按“依赖成本从低到高”排列。
前两种方式不需要用户装任何额外的东西。

## 方式一：直接运行源码（开发者）

```bash
git clone <repo>
cd todoseine
python -m pip install -e .
c2t --version
```

需要 Python 3.9+。`pip install -e .` 只是把 `c2t` 命令挂进 PATH，不下载任何依赖包
（`dependencies = []`）。

## 方式二：单文件 .pyz（推荐给普通用户）

```bash
python tools/build_pyz.py        # 产出 dist/c2t.pyz（约 80 KB）
python dist/c2t.pyz --help
```

`.pyz` 是 Python 官方的 zipapp 格式：一个文件、双击即可用 `python` 运行，
不需要 `pip install`，不需要虚拟环境。缺点是目标机器上要有 Python 3.9+。

给用户的一句话说明：

> 装 Python，把 `c2t.pyz` 放到任意目录，运行 `python c2t.pyz import 聊天记录.txt`。

## 方式三：Qt 看板 exe（当前主推）

```bash
python -m pip install -e ".[qt]" pyinstaller
python -m PyInstaller --noconfirm --clean todoseine_qt.spec
```

产物是 `dist/todoseine_qt.exe`（当前约 51 MB，Windows 11 x64）：

- 双击即用，不需要 Python；启动后常驻托盘，看板可随时唤出；
- 入口是 `tools/qt_launcher.py`。不能把 `qt/main.py` 直接当入口——PyInstaller
  会以 `__main__` 执行它，包内相对导入会失败；
- `console=False`：不弹黑框，日志写 `%USERPROFILE%\.chat2todo\app.log`；
- 图标 `assets/todoseine.ico` 与版本资源 `tools/version_info.txt`；
- spec 里的 Qt DLL 是**白名单**（Core / Gui / Widgets / Network / Svg / OpenGL），
  PySide6 自带的 Quick、Qml、3D、Designer 一共 80 MB+，不白名单化会直接翻倍。

两个容易踩的点：

1. Qt6 的 DLL 直接放在 `PySide6` 根目录，spec 必须把它们收进去；
2. onefile 解压到 `_MEIPASS` 后要 `os.add_dll_directory`，
   这一步在 `tools/runtime_hook_qt.py` 里做。

未签名的 exe 会被部分杀毒软件误报，这是 Windows 的普遍现象，与本项目行为无关。

## 桌面看板

看板是 PySide6 写的（`src/todoseine/qt/`），只支持 Windows 11：

- 无边框 + DWM 圆角 / Mica，位置尺寸自动记忆，拖边改宽、贴边吸附；
- 托盘常驻，托盘菜单可暂停监听、立即同步、开关自启；
- 全局热键：`Ctrl+Alt+D` 唤出/收起看板，`Ctrl+Shift+S` 框选截图建待办；
- A 阶段起 `c2t dash` 直接启动 Qt 版看板，旧 tkinter 看板已从仓库移除。

## 版本与数据迁移

- 版本号遵循语义化版本；
- 数据库带 `schema_version` 记录，升级时自动补齐缺失的表与索引；
- 降级不保证兼容：新版本写过的库，旧版本可能读不了，请先备份。

## 升级

```bash
git pull
python tools/build_pyz.py        # 重新生成 pyz
c2t doctor                        # 确认环境仍然正常
```
