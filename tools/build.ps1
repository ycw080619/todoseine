# Chat2Todo - PyInstaller build script
# Usage: 从任意目录执行 & "D:\QQ 待办项目\tools\build.ps1"
# Output: dist\chat2todo.exe (onefile, no console window)

$repo = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $repo "tools\chat2todo-launcher.py"
$consoleDir = Join-Path $repo "tools\qq_db_export"
$distDir = Join-Path $repo "dist"
$buildDir = Join-Path $repo "build"
$exe = Join-Path $distDir "chat2todo.exe"

function Exec-Checked {
    param(
        [string] $Desc,
        [scriptblock] $Script
    )
    & $Script
    if ($LASTEXITCODE -ne 0) {
        throw "[$Desc] 失败 (exit $LASTEXITCODE)"
    }
}

Write-Host "=== Chat2Todo build ===" -ForegroundColor Cyan
Write-Host "Repo: $repo"

$prevCwd = Get-Location
try {
    Set-Location $repo

    # 1. Dependencies
    Write-Host "[1/4] pip install -e . + pyinstaller"
    Exec-Checked "pip upgrade" { python -m pip install --upgrade pip 2>$null | Out-Null }
    Exec-Checked "pip install" { python -m pip install -e $repo pyinstaller 2>$null | Out-Null }

    # 2. Clean
    Write-Host "[2/4] clean build/ dist/"
    Remove-Item -Recurse -Force $buildDir -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $distDir -ErrorAction SilentlyContinue

    # 3. Build (all paths absolute, cwd-independent)
    Write-Host "[3/4] PyInstaller onefile windowed..."
    python -m PyInstaller --noconfirm --onefile --windowed `
        --name chat2todo `
        --collect-all chat2todo `
        --paths $consoleDir `
        --hidden-import console `
        --hidden-import sqlite3 `
        --hidden-import tkinter `
        --hidden-import tkinter.ttk `
        --hidden-import tkinter.filedialog `
        --hidden-import tkinter.messagebox `
        --hidden-import tkinter.scrolledtext `
        --hidden-import win32api `
        --hidden-import win32crypt `
        $launcher 2>&1 | Select-Object -Last 5

    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }

    # 4. Verify
    if (Test-Path $exe) {
        $mb = [math]::Round((Get-Item $exe).Length / 1MB, 2)
        Write-Host "[4/4] OK dist\chat2todo.exe ($mb MB)" -ForegroundColor Green
    } else {
        Write-Host "[4/4] FAIL exe not found" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "BUILD FAILED: $_" -ForegroundColor Red
    exit 1
} finally {
    Set-Location $prevCwd
}

Write-Host "Done." -ForegroundColor Cyan
