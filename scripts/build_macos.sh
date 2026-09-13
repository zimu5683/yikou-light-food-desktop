#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
python3 scripts/build_frontend.py

# 下载并校验 WPS 云文档同步组件（见 vendor/kdocs-cli/README.md）。
python3 scripts/fetch_kdocs_cli.py

# 内置 Chromium 打进 .app 的 Contents/Resources/browser/，不打进单文件
# 可执行文件；这个变量让 PyInstaller 的 Playwright hook 不要连带收集本机
# 浏览器缓存。
export PLAYWRIGHT_BROWSERS_PATH=0
python3 -m PyInstaller --clean --noconfirm "yikou-light-food.spec"

APP="$ROOT/dist/yikou-light-food.app"
if [[ ! -d "$APP" ]]; then
  echo "PyInstaller did not create $APP" >&2
  exit 1
fi

# 内置浏览器：放进 .app 的 Resources，运行时按 Contents/Resources/browser
# 解析；更换 .app 的更新流程会连同浏览器一起替换。
python3 scripts/fetch_browser.py
rm -rf "$APP/Contents/Resources/browser"
mkdir -p "$APP/Contents/Resources"
cp -a "$ROOT/vendor/browser" "$APP/Contents/Resources/browser"

du -sh "$APP" | awk '{print "App bundle size: " $1}'
echo "Build complete: ${APP}（含内置 Chromium）"
