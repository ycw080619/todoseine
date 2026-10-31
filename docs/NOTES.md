# 开发踩坑笔记

> 这些都是真实踩过的坑，按主题归类。给后来者省时间。

---

## 测试与环境

1. **系统 Python 跑测试是假绿**：Qt 用例被 `importorskip` 整体跳过，"测试通过"必须来自装了 PySide6 的环境。验证方式：确认 `test_qt_smoke.py` 是真跑了而不是 skip。
2. **改完 GUI 要重启常驻进程**：自启脚本拉起的进程加载的是启动那一刻的代码，热重载不覆盖已加载模块。
3. **`pythonw` 看不到输出**：调试时改用 `python.exe` 并重定向，且加 `-u` 否则 `print` 被块缓冲。
4. **GUI 自动化测试的坑**：窗口贴在桌面层时，真实点击会被上层窗口吃掉，改用 `event_generate`；真实点击触发 `render()` 重建 item 后，第二次点击常落空。

## Windows / bat / 编码

5. **`.bat` 含中文路径必须先 `chcp 65001`**，否则 cmd 按 GBK 解码路径乱码，`PYTHONPATH` 失效、`pythonw` 静默退出（没控制台，看不到报错）。
6. **`.bat` 必须 CRLF + UTF-8 BOM**：LF 换行会让 cmd 解析失败弹出"不是内部或外部命令"；缺 BOM 时中文 UTF-8 字节被 GBK 误读，含 `&`/`>`/`^` 的命令被错误拆分。
7. **杀进程别用命令行通配**：`Where CommandLine -like '*xxx*'` 会把执行命令的 shell 自己匹配上，一杀就退出码异常。要按进程名过滤 + 排除当前 PID。
8. **PowerShell 的 `Select-String -Pattern` 正则含引号会被吞**，复杂正则改用 ripgrep。

## Tk / Win32

9. **tk 窗口句柄要用 `GetAncestor(hwnd, GA_ROOT)`**：`winfo_id()` 返回的是子窗口，`GetParent` 未必是顶层，`SetWindowPos` 作用在错的句柄上会"代码执行了但窗口纹丝不动"。
10. **DPI 感知必须在创建任何 Tk 窗口之前设置**：否则逻辑像素和物理像素不一致，截图区域整个错位。
11. **验证热键/置顶是否真生效**：看 `GetWindowLong(hwnd, -20)` 是否含 `WS_EX_TOPMOST`，别只看 Z 序。
12. **单实例互斥探测用 `OpenMutexW` 而不是 `CreateMutexW`**：后者会让自己的单实例判据误判"已在运行"而退出。
13. **多个控件 `pack(side="right")` 到同一父容器会挤成一行互相覆盖**，要嵌套 Frame 再 `pack(side="top")`。
14. **`root.after(ms, func)` 写在 `def func` 之前** → `UnboundLocalError`，表现为"窗口起不来、连 pid 都没有"。

## Qt / PySide6

15. **迷你胶囊展开后回不到原尺寸**：只恢复 `setMinimumSize` 不够，布局已按 `sizeHint` 撑高，必须在动画结束回调里显式 `setGeometry`。
16. **打包时 `hiddenimports` 必须含 `sqlcipher3` 与 `sqlcipher3.dbapi2`**，否则打出来的包读不了 QQ 库。

## 数据库 / QQ

17. **SQLCipher 的 key 格式**：口令必须写成单引号字符串；写成 `PRAGMA key = "x'...'"`（十六进制）会报 `hmac check failed`，看起来像密钥错，其实是格式错。
18. **PRAGMA 顺序严格**：`cipher_page_size` → `key` → `cipher_kdf_iter` → `cipher_hmac_algorithm` → `cipher_kdf_algorithm`，顺序错了解密失败。
19. **QQ 库文件头 1024 字节是自定义头**，要剥掉再交给 SQLCipher。

## 配置 / 代码

20. **`config.load()` 的默认查找路径不含用户配置目录**，必须显式传路径，否则读到的永远是默认值——"改了配置不生效"排查很久。
21. **参数名别和局部变量重名**：热键录制函数里形参 `key` 被 `key = e.keysym.lower()` 遮蔽，结果把按键字母当配置项写脏，热键"怎么录都不生效"。
22. **配置写入函数只写指定段**：写错段名会静默新增一个空段，不会报错。
23. **文本替换脚本里 `assert old in s` 失败时外层不检查**，`git add/commit` 仍会执行，改动被静默跳过 → 要检查退出码。
24. **计数字段与可见行必须同源**：用 `open_count` 却列出含当天完成项的 `items` 会造成"数字 1、列表两行"。
25. **AI 这类"慢但可选"的增强不要放在 UI 阻塞路径**：先出结果、后台增强、可中止。
