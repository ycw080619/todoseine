@echo off
chcp 65001 >nul
rem 供 Windows 计划任务调用的后台同步：不弹看板、只跑数据
set PY=C:\Users\ycw\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe
set TOOLS=C:\Users\ycw\.dsh\dsh_wechat\chat2todo\tools\qq_db_export
"%PY%" "%TOOLS%\sync.py" --days 1 --max-ai 80 >> "%USERPROFILE%\Chat2Todo\sync.log" 2>&1
