#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

# Keep browser installation outside the bundle.  The application can
# download Chromium on first run when Edge/Chromium is unavailable.
export PLAYWRIGHT_BROWSERS_PATH=0
python3 -m PyInstaller --clean --noconfirm "yikou-light-food.spec"

BIN="$ROOT/dist/yikou-light-food"
if [[ ! -x "$BIN" ]]; then
  echo "PyInstaller did not create $BIN" >&2
  exit 1
fi

case "$(uname -m)" in
  x86_64) ARCH_TAG="x64" ;;
  aarch64 | arm64) ARCH_TAG="arm64" ;;
  *) ARCH_TAG="$(uname -m)" ;;
esac

# Relative names keep the checksum file usable after download, wherever the
# user extracts it; an absolute path would leak the CI workspace location.
NAME="yikou-light-food-linux-${ARCH_TAG}.tar.gz"
tar -czf "$NAME" -C "$ROOT/dist" "yikou-light-food"
sha256sum "$NAME" > "$NAME.sha256"
echo "Build complete: $ROOT/$NAME"
