@echo off
rem 截图待办：先 Win+Shift+S 框选截图，再双击本文件。路径自解析，无需 pip install
for %%i in ("%~dp0..\..") do set "PYTHONPATH=%%~fi\src"
start "" pyw -3 "%~dp0snap.py" %*
