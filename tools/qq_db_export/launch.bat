@echo off
chcp 65001 >nul
rem 桌面图标入口：看板没开就开看板，已开就开设置
set PYTHONPATH=D:\QQ 待办项目\src
start "" pyw -3 "D:\QQ 待办项目\tools\qq_db_export\launch.py"
