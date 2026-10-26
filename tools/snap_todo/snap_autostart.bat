@echo off
chcp 65001 >nul
rem auto-start screenshot hotkey (no console window)
set PYTHONPATH=D:\QQ 待办项目\src
start "" pyw -3 "D:\QQ 待办项目\tools\snap_todo\snap.py" --hotkey --db "C:\Users\ycw\Chat2Todo\qq.db"
