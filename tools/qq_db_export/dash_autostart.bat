@echo off
chcp 65001 >nul
rem auto-start dashboard (no console window)

rem === Python 自解析：逐个验证 PySide6，PATH 首条可能是缺 Qt 依赖的运行时 ===
set "PY_CMD="
for /f "delims=" %%i in ('where pythonw 2^>nul') do (
    if not defined PY_CMD (
        "%%i" -c "import PySide6" >nul 2>&1 && set "PY_CMD=%%i"
    )
)
for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PY_CMD (
        "%%i" -c "import PySide6" >nul 2>&1 && set "PY_CMD=%%i"
    )
)
if not defined PY_CMD (
    for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
        if not defined PY_CMD set "PY_CMD=%%i"
    )
)
if not defined PY_CMD (
    echo [TodoSeine] 未找到可用 Python（需 3.9+ 且已 pip install PySide6）
    pause
    exit /b 1
)

cd /d "%~dp0..\.."
set PYTHONPATH=%~dp0..\..\src
start "" "%PY_CMD%" -m todoseine dash
