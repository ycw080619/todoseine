# TodoSeine 一键构建脚本：PyInstaller 打包 exe → Inno Setup 生成安装包
# 用法：& ".\tools\build_installer.ps1"
# 产物：
#   dist\todoseine_qt\               （one-dir 程序文件夹，含 todoseine_qt.exe + DLL）
#   dist\TodoSeine-Setup-0.4.1.exe  （一键安装包，给小白用户）

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$distDir = Join-Path $repo "dist"
$iscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"

Write-Host "=== TodoSeine 一键构建 ===" -ForegroundColor Cyan

# 1. PyInstaller 打包
Write-Host "[1/3] PyInstaller 打包 todoseine_qt.exe ..." -ForegroundColor Cyan
Push-Location $repo
try {
    & ".venv\Scripts\pyinstaller.exe" todoseine_qt.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败 (exit $LASTEXITCODE)" }
} finally {
    Pop-Location
}

$exe = Join-Path $distDir "todoseine_qt\todoseine_qt.exe"
if (-not (Test-Path $exe)) { throw "未找到 $exe" }
$exeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "      todoseine_qt\ ($exeMb MB)" -ForegroundColor Green

# 2. Inno Setup 生成安装包
Write-Host "[2/3] Inno Setup 编译安装包 ..." -ForegroundColor Cyan
if (-not (Test-Path $iscc)) {
    throw "未找到 ISCC.exe，请先安装 Inno Setup 6"
}
& $iscc (Join-Path $PSScriptRoot "installer.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败 (exit $LASTEXITCODE)" }

# 3. 验证产物
Write-Host "[3/3] 验证产物 ..." -ForegroundColor Cyan
$setup = Join-Path $distDir "TodoSeine-Setup-0.4.1.exe"
if (-not (Test-Path $setup)) { throw "未找到 $setup" }
$setupMb = [math]::Round((Get-Item $setup).Length / 1MB, 1)
Write-Host ""
Write-Host "构建完成！" -ForegroundColor Green
Write-Host "  程序：  $exe ($exeMb MB)"
Write-Host "  安装包：$setup ($setupMb MB)"
Write-Host ""
