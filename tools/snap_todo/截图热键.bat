@echo off
chcp 65001 >nul
rem 截图待办热键常驻：双击后 Ctrl+Alt+S 随时唤出截图待办（记得先 Win+Shift+S 截图）
rem 关闭方法：任务管理器结束 pythonw.exe，或注销重登
rem pythonw.exe 优先（无黑窗）

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
start "" "%PY_CMD%" "%~dp0snap.py" --hotkey
