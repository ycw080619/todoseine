@echo off
chcp 65001 >nul
rem Chat2Todo 后台同步：供 Windows 计划任务调用（控制台「定时同步」按钮会自动注册）
rem 内置引擎直读 QQ 数据库，无需 Docker；日志追加到 %USERPROFILE%\Chat2Todo\sync.log

rem === Python 自解析 ===
set "PY_CMD="
where py >nul 2>&1
if %errorlevel%==0 (
    set "PY_CMD=py -3"
) else (
    where python >nul 2>&1
    if %errorlevel%==0 (
        for /f "delims=" %%i in ('where python 2^>nul') do set "PY_CMD=%%i"
    )
)
if "%PY_CMD%"=="" (
    echo [Chat2Todo] 未找到 Python，请先安装 Python 3.9+ 或确认 PATH
    exit /b 1
)

cd /d "%~dp0..\.."
set PYTHONPATH=%~dp0..\..\src
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
%PY_CMD% -u -m chat2todo --config "%USERPROFILE%\Chat2Todo\c2t.toml" db-import --days 1 --db "%USERPROFILE%\Chat2Todo\qq.db" >> "%USERPROFILE%\Chat2Todo\sync.log" 2>&1
