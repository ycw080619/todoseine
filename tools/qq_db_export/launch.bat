@echo off
chcp 65001 >nul
rem 桌面图标入口：看板没开就开看板，已开就开设置
rem 说明：pythonw.exe 优先（无黑窗），py launcher 某些机器上 -3w 找不到 runtime

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
start "" "%PY_CMD%" "%~dp0launch.py"
