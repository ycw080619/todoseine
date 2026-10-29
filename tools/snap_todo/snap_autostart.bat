@echo off
chcp 65001 >nul
rem auto-start screenshot hotkey (no console window)
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
if not defined PY_CMD exit /b 1

for %%i in ("%~dp0..\..") do set "REPO=%%~fi"
set PYTHONPATH=%REPO%\src
start "" "%PY_CMD%" "%~dp0snap.py" --hotkey --db "%USERPROFILE%\Chat2Todo\qq.db"
