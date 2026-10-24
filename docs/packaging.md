# 分发与打包

Chat2Todo 的核心承诺是**零第三方依赖**，因此分发方式按“依赖成本从低到高”排列。
前两种方式不需要用户装任何额外的东西。

## 方式一：直接运行源码（开发者）

```bash
git clone <repo>
cd chat2todo
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

## 方式三：独立 exe（可选，需额外工具）

如果目标用户连 Python 都没有，可以用 PyInstaller 打包成 exe：

```bash
python -m pip install pyinstaller
pyinstaller --onefile --name c2t -p src src/chat2todo/__main__.py
```

注意三点，写在这里是为了不误伤“零依赖”的承诺：

1. PyInstaller 是**构建期**工具，不进运行时依赖；
2. 产物体积约 8–12 MB（onedir 更小，启动更快）；
3. 未签名的 exe 会被部分杀毒软件误报，这是 Windows 的普遍现象，与本项目行为无关。

## 桌面看板

`c2t dash` 使用 Python 标准库的 tkinter。绝大多数官方 Python 安装包自带它，
但少数精简发行版（如某些 Linux 的 `python3-minimal`）需要单独安
`python3-tk`。`c2t doctor` 不检查这一项，因为看板是可选能力，
缺失时只影响 `dash` 命令，其余功能不受影响。

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
