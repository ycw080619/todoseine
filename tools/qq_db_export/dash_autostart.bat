@echo off
chcp 65001 >nul
rem auto-start dashboard (no console); pythonw = pythonw launcher

rem === Python 自解析 ===
set "PY_CMD="
where py >nul 2>&1
if %errorlevel%==0 (
    set "PY_CMD=py -3w"
) else (
    where pythonw >nul 2>&1
    if %errorlevel%==0 (
        for /f "delims=" %%i in ('where pythonw 2^>nul') do set "PY_CMD=%%i"
    )
)
if "%PY_CMD%"=="" (
    echo [Chat2Todo] 未找到 Python，请先安装 Python 3.9+ 或确认 PATH
    pause & exit /b 1
)

cd /d "%~dp0..\.."
set PYTHONPATH=%~dp0..\..\src
start "" %PY_CMD% -m chat2todo dash --db "%USERPROFILE%\Chat2Todo\qq.db" --interval 30
