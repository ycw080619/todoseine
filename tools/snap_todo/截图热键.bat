@echo off
chcp 65001 >nul
rem 截图待办热键常驻：双击后 Ctrl+Alt+S 随时唤出截图待办（记得先 Win+Shift+S 截图）
rem 关闭方法：任务管理器结束 pythonw.exe，或注销重登
rem pythonw.exe 优先（无黑窗）

rem === Python 自解析：逐个验证 tkinter，PATH 首条可能是无 tk 的运行时 ===
set "PY_CMD="
for /f "delims=" %%i in ('where pythonw 2^>nul') do (
    if not defined PY_CMD (
        "%%i" -c "import tkinter" >nul 2>&1 && set "PY_CMD=%%i"
    )
)
for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PY_CMD (
        "%%i" -c "import tkinter" >nul 2>&1 && set "PY_CMD=%%i"
    )
)
if not defined PY_CMD (
    for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
        if not defined PY_CMD set "PY_CMD=%%i"
    )
)
if not defined PY_CMD (
    echo [TodoSeine] 未找到可用 Python（需 3.9+ 且带 tkinter），请安装并勾选 Add to PATH
    pause
    exit /b 1
)

for %%i in ("%~dp0..\..") do set "PYTHONPATH=%%~fi\src"
start "" "%PY_CMD%" "%~dp0snap.py" --hotkey
