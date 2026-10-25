@echo off
chcp 65001 >nul
title Chat2Todo 一键同步
"C:\Users\ycw\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" "C:\Users\ycw\.dsh\dsh_wechat\chat2todo\tools\qq_db_export\sync.py" %*
echo.
pause