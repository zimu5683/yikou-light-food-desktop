<#!
.SYNOPSIS
    Build the Windows executable with PyInstaller.
#>
$ErrorActionPreference = "Stop"

# Python 子进程统一按 UTF-8 输出，避免 Windows 默认 cp1252 在中文进度日志上
# 抛 UnicodeEncodeError。运行器支持时让原生命令的非零退出码直接中断构建。
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $true
}
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python scripts/build_frontend.py

# 下载并校验 WPS 云文档同步组件（见 vendor/kdocs-cli/README.md）。
python scripts/fetch_kdocs_cli.py
if ($LASTEXITCODE -ne 0) { throw "fetch_kdocs_cli.py failed ($LASTEXITCODE)" }

# 内置 Chromium 走 exe 同级的 browser\ 目录，不打进单文件 exe；这个变量让
# PyInstaller 的 Playwright hook 不要把本机浏览器缓存也收进去。
$env:PLAYWRIGHT_BROWSERS_PATH = "0"
python -m PyInstaller --clean --noconfirm "yikou-light-food.spec"

$exe = Join-Path $root "dist\yikou-light-food.exe"
if (-not (Test-Path $exe)) { throw "PyInstaller did not create $exe" }
$sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "Executable size: $sizeMb MB (内置 Chromium 在 browser\ 目录中)"

# 内置浏览器：抓取并精简 Chromium，然后与 exe 一起打成发行 zip。
python scripts/fetch_browser.py
if ($LASTEXITCODE -ne 0) { throw "fetch_browser.py failed ($LASTEXITCODE)" }

$bundle = Join-Path $root "dist\yikou-light-food-windows"
if (Test-Path $bundle) { Remove-Item -Recurse -Force $bundle }
New-Item -ItemType Directory -Path $bundle | Out-Null
Copy-Item $exe $bundle
Copy-Item -Recurse (Join-Path $root "vendor\browser") (Join-Path $bundle "browser")

$zip = Join-Path $root "dist\yikou-light-food-windows-x64.zip"
Compress-Archive -Path (Join-Path $bundle "*") -DestinationPath $zip -Force

$zipMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "Install package: $zip ($zipMb MB)"

Write-Host "Build complete: $root\dist\yikou-light-food.exe"
Write-Host "首次安装请分发 ${zip}（含内置浏览器的完整解压包）"
