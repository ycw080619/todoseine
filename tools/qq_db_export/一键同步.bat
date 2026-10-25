@echo off
chcp 65001 >nul
title Chat2Todo 一键同步
rem 默认行为：同步（解密→入库→AI 复核）跑完后自动打开看板
"C:\Users\ycw\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" "C:\Users\ycw\.dsh\dsh_wechat\chat2todo\tools\qq_db_export\sync.py" --open-dash %*
echo.
pause
