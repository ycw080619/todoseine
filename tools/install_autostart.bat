@echo off
chcp 65001 >nul
rem 一键重建 TodoSeine 自启 + 桌面图标（搬到新目录后跑一次即可）
rem 用法：双击本文件，或在 cmd 里运行

set "PROJ=%~dp0.."
set "EXE=%PROJ%\dist\todoseine_qt.exe"

if not exist "%EXE%" (
    echo [错误] 找不到 %EXE%
    echo 请先运行： .venv\Scripts\python.exe -m PyInstaller todoseine_qt.spec --noconfirm
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$exe='%EXE%'; $work='%PROJ%';" ^
  "$ws=New-Object -ComObject WScript.Shell;" ^
  "$lnk1=Join-Path ([Environment]::GetFolderPath('Startup')) 'TodoSeine.lnk';" ^
  "$s=$ws.CreateShortcut($lnk1); $s.TargetPath=$exe; $s.WorkingDirectory=$work; $s.IconLocation=\"$exe,0\"; $s.Save();" ^
  "$lnk2=Join-Path ([Environment]::GetFolderPath('Desktop')) 'TodoSeine.lnk';" ^
  "$s2=$ws.CreateShortcut($lnk2); $s2.TargetPath=$exe; $s2.WorkingDirectory=$work; $s2.IconLocation=\"$exe,0\"; $s2.Save();" ^
  "Write-Host '已更新自启:' $lnk1; Write-Host '已更新桌面图标:' $lnk2"

echo.
echo 完成。下次开机将自动启动 TodoSeine。
pause
