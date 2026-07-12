#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

# Keep browser installation outside the app bundle.  The application can
# download Chromium on first run when Edge/Chromium is unavailable.
export PLAYWRIGHT_BROWSERS_PATH=0
python3 -m PyInstaller --clean --noconfirm "一口轻食.spec"

echo "Build complete: $ROOT/dist/yikou-light-food"
