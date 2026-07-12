<#!
.SYNOPSIS
    Build the Windows executable with PyInstaller.
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Browser binaries are installed into the user cache at first run.  Setting
# this variable keeps PyInstaller from trying to bundle a machine-specific
# browser into the executable.
$env:PLAYWRIGHT_BROWSERS_PATH = "0"
python -m PyInstaller --clean --noconfirm "yikou-light-food.spec"

$exe = Join-Path $root "dist\yikou-light-food.exe"
if (-not (Test-Path $exe)) { throw "PyInstaller did not create $exe" }
$sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "Executable size: $sizeMb MB (Playwright browsers are external)"

Write-Host "Build complete: $root\dist\yikou-light-food.exe"

