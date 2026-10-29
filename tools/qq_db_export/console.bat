@echo off
chcp 65001 >nul
rem Chat2Todo 控制台：双击启动（无黑窗）

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
    echo [Chat2Todo] 未找到可用 Python（需 3.9+ 且带 tkinter），请安装并勾选 Add to PATH
    pause
    exit /b 1
)

cd /d "%~dp0..\.."
set PYTHONPATH=%~dp0..\..\src
start "" "%PY_CMD%" "%~dp0console.py"
