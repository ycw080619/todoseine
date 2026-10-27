@echo off
chcp 65001 >nul
rem 桌面图标入口：看板没开就开看板，已开就开设置
cd /d "D:\QQ 待办项目"
set PYTHONPATH=D:\QQ 待办项目\src
start "" "C:\Program Files\Python312\pythonw.exe" "D:\QQ 待办项目\tools\qq_db_export\launch.py"
