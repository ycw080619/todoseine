@echo off
chcp 65001 >nul
rem auto-start dashboard (no console); pythonw = pythonw launcher
cd /d "D:\QQ 待办项目"
set PYTHONPATH=D:\QQ 待办项目\src
start "" "C:\Program Files\Python312\pythonw.exe" -m chat2todo dash --db "C:\Users\ycw\Chat2Todo\qq.db" --interval 30
