@echo off
chcp 65001 >nul
rem Chat2Todo 控制台：双击启动（无黑窗）

rem === Python 自解析 ===
set "PY_CMD="
for /f "delims=" %%i in ('where pythonw 2^>nul') do (
    set "PY_CMD=%%i"
    goto :found
)
:found
if "%PY_CMD%"=="" (
    echo [Chat2Todo] 未找到 pythonw.exe，请安装 Python 3.9+ 并勾选 Add to PATH
    pause & exit /b 1
)

cd /d "%~dp0..\.."
set PYTHONPATH=%~dp0..\..\src
start "" "%PY_CMD%" "%~dp0console.py"
