@echo off
chcp 65001 >nul
rem 截图待办：先 Win+Shift+S 框选截图，再双击本文件。路径自解析，无需 pip install
rem pythonw.exe 优先（无黑窗），py launcher 某些机器上 -3w 找不到 runtime

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

for %%i in ("%~dp0..\..") do set "PYTHONPATH=%%~fi\src"
start "" "%PY_CMD%" "%~dp0snap.py" %*
