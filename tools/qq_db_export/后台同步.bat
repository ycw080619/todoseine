@echo off
chcp 65001 >nul
rem Chat2Todo 后台同步：供 Windows 计划任务调用（控制台「定时同步」按钮会自动注册）
rem 内置引擎直读 QQ 数据库，无需 Docker；日志追加到 %USERPROFILE%\Chat2Todo\sync.log
rem python.exe 优先（CLI 有窗口），不依赖 py launcher

rem === Python 自解析 ===
set "PY_CMD="
for /f "delims=" %%i in ('where python 2^>nul') do (
    set "PY_CMD=%%i"
    goto :found
)
:found
if "%PY_CMD%"=="" (
    echo [Chat2Todo] 未找到 python.exe，请安装 Python 3.9+ 并勾选 Add to PATH >> "%USERPROFILE%\Chat2Todo\sync.log" 2>&1
    exit /b 1
)

cd /d "%~dp0..\.."
set PYTHONPATH=%~dp0..\..\src
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
"%PY_CMD%" -u -m chat2todo --config "%USERPROFILE%\Chat2Todo\c2t.toml" db-import --days 1 --db "%USERPROFILE%\Chat2Todo\qq.db" >> "%USERPROFILE%\Chat2Todo\sync.log" 2>&1
