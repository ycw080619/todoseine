@echo off
chcp 65001 >nul
rem Chat2Todo 后台同步：供 Windows 计划任务调用（控制台「定时同步」按钮会自动注册）
rem 内置引擎直读 QQ 数据库，无需 Docker；日志追加到 %USERPROFILE%\Chat2Todo\sync.log
rem 绝对 Python 路径避免 py launcher 在计划任务环境下找不到
cd /d "D:\QQ 待办项目"
set PYTHONPATH=D:\QQ 待办项目\src
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
"C:\Program Files\Python312\python.exe" -u -m chat2todo --config "%USERPROFILE%\Chat2Todo\c2t.toml" db-import --days 1 --db "%USERPROFILE%\Chat2Todo\qq.db" >> "%USERPROFILE%\Chat2Todo\sync.log" 2>&1
