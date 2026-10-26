@echo off
rem 截图待办热键常驻：双击后 Ctrl+Alt+S 随时唤出截图待办（记得先 Win+Shift+S 截图）
rem 关闭方法：任务管理器结束 pyw.exe，或注销重登
for %%i in ("%~dp0..\..") do set "PYTHONPATH=%%~fi\src"
start "" pyw -3 "%~dp0snap.py" --hotkey
