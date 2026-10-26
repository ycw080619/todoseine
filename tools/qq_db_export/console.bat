@echo off
rem Chat2Todo 控制台：双击启动。路径自解析，无需 pip install（PYTHONPATH 指向仓库 src）
for %%i in ("%~dp0..\..") do set "PYTHONPATH=%%~fi\src"
start "" pyw -3 "%~dp0console.py"
