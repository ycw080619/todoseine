@echo off
chcp 65001 >nul
rem auto-start screenshot hotkey (no console window)
rem pythonw.exe 优先（无黑窗）

rem === Python 自解析 ===
set "PY_CMD="
for /f "delims=" %%i in ('where pythonw 2^>nul') do (
    set "PY_CMD=%%i"
    goto :found
)
:found
if "%PY_CMD%"=="" (
    exit /b 1
)

for %%i in ("%~dp0..\..") do set "REPO=%%~fi"
set PYTHONPATH=%REPO%\src
start "" "%PY_CMD%" "%~dp0snap.py" --hotkey --db "%USERPROFILE%\Chat2Todo\qq.db"
