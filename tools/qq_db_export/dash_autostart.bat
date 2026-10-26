@echo off
chcp 65001 >nul
rem auto-start dashboard (no console); pyw = pythonw launcher
set PYTHONPATH=D:\QQ 待办项目\src
start "" pyw -3 -m chat2todo dash --db "C:\Users\ycw\Chat2Todo\qq.db" --interval 30
