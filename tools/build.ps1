# TodoSeine - PyInstaller build script
# Usage: 从任意目录执行 & "D:\QQ 待办项目\tools\build.ps1"
# Output: dist\todoseine.exe (onefile, no console window)

$repo = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $repo "tools\todoseine-launcher.py"
$consoleDir = Join-Path $repo "tools\qq_db_export"
$distDir = Join-Path $repo "dist"
$buildDir = Join-Path $repo "build"
$exe = Join-Path $distDir "todoseine.exe"

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

Write-Host "=== TodoSeine build ===" -ForegroundColor Cyan
Write-Host "Repo: $repo"

$prevCwd = Get-Location
try {
    Set-Location $repo

    # 1. Dependencies
    Write-Host "[1/4] pip install -e . + pyinstaller"
    # pip 往 stderr 写 warning（如 "script not on PATH"），2>$null 吞不掉
    # 因为 PowerShell 把 stderr 当作 NativeCommandError 收集，
    # 用 *>&1 把 stderr 合并进 stdout 再 Out-Null
    Exec-Checked "pip upgrade" { python -m pip install --upgrade pip *>&1 | Out-Null }
    Exec-Checked "pip install" { python -m pip install -e $repo pyinstaller *>&1 | Out-Null }

    # 2. Clean
    Write-Host "[2/4] clean build/ dist/"
    Remove-Item -Recurse -Force $buildDir -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $distDir -ErrorAction SilentlyContinue

    # 3. Build (all paths absolute, cwd-independent)
    Write-Host "[3/4] PyInstaller onefile windowed..."
    $toolsDir = Join-Path $repo "tools"
    $addDataVal = Join-Path $toolsDir ""
    $addDataVal = "$toolsDir;tools"
    $pyArgs = @(
        "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--windowed",
        "--name", "todoseine",
        "--collect-all", "todoseine",
        "--paths", $consoleDir,
        "--hidden-import", "console",
        "--hidden-import", "sqlite3",
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "tkinter.messagebox",
        "--hidden-import", "tkinter.scrolledtext",
        "--hidden-import", "win32api",
        "--hidden-import", "win32crypt",
        "--add-data", $addDataVal,
        $launcher
    )
    & python $pyArgs 2>&1 | Select-Object -Last 5

    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }

    # 4. Verify
    if (Test-Path $exe) {
        $mb = [math]::Round((Get-Item $exe).Length / 1MB, 2)
        Write-Host "[4/4] OK dist\todoseine.exe ($mb MB)" -ForegroundColor Green
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
