@echo off
chcp 65001 >nul
rem Chat2Todo 控制台：双击启动。路径自解析，无需 pip install（PYTHONPATH 指向仓库 src）
cd /d "%~dp0..\.."
set PYTHONPATH=%~dp0..\..\src
start "" "C:\Program Files\Python312\pythonw.exe" "%~dp0console.py"
