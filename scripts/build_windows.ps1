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

Write-Host "Build complete: $root\dist\yikou-light-food.exe"

