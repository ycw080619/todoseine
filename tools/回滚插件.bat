@echo off
chcp 65001 >nul
title Chat2Todo 插件一键回滚
echo ============================================
echo   Chat2Todo 插件一键回滚
echo   作用：恢复到最近一次「安装/启用插件前」的快照
echo         并退出插件安全模式（不触碰数据库）
echo ============================================
echo.

where c2t >nul 2>nul
if %errorlevel%==0 (
    c2t plugin-rollback
    goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python -m chat2todo plugin-rollback
    goto done
)

echo 未找到 c2t 命令，也没有可用的 Python。
echo 请先安装 Chat2Todo，或在本目录用 PyInstaller 打包后的 exe 手动执行：
echo     chat2todo.exe plugin-rollback

:done
echo.
pause
