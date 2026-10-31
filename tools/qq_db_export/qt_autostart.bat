@echo off
chcp 65001 >nul
rem TodoSeine Qt 版开机自启：优先用打包好的 exe，否则降级到源码 .venv
set "EXE=%~dp0..\..\dist\todoseine_qt.exe"
if exist "%EXE%" (
    start "" "%EXE%"
    exit /b 0
)
rem 降级：用 .venv 跑源码版（静默，失败不弹窗）
set "VENV=%~dp0..\..\.venv\Scripts\pythonw.exe"
if exist "%VENV%" (
    cd /d "%~dp0..\.."
    set PYTHONPATH=%~dp0..\..\src
    start "" "%VENV%" -m todoseine.qt.main
)
