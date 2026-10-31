@echo off
chcp 65001 >nul
rem TodoSeine 后台同步：供 Windows 计划任务调用（控制台「定时同步」按钮会自动注册）
rem 内置引擎直读 QQ 数据库，无需 Docker；日志追加到 %USERPROFILE%\Chat2Todo\sync.log
rem python.exe 优先（CLI 有窗口），不依赖 py launcher

rem === Python 自解析：逐个验证可运行，PATH 首条可能是坏运行时 ===
set "PY_CMD="
for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PY_CMD (
        "%%i" -c "import sqlite3" >nul 2>&1 && set "PY_CMD=%%i"
    )
)
if not defined PY_CMD (
    for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
        if not defined PY_CMD set "PY_CMD=%%i"
    )
)
if not defined PY_CMD (
    echo [TodoSeine] 未找到 python.exe，请安装 Python 3.9+ 并勾选 Add to PATH >> "%USERPROFILE%\Chat2Todo\sync.log" 2>&1
    exit /b 1
)

cd /d "%~dp0..\.."
set PYTHONPATH=%~dp0..\..\src
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
"%PY_CMD%" -u -m todoseine --config "%USERPROFILE%\Chat2Todo\c2t.toml" db-import --days 1 --db "%USERPROFILE%\Chat2Todo\qq.db" >> "%USERPROFILE%\Chat2Todo\sync.log" 2>&1
