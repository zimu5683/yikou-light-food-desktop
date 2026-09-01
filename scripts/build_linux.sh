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

TARBALL="$ROOT/yikou-light-food-linux-${ARCH_TAG}.tar.gz"
tar -czf "$TARBALL" -C "$ROOT/dist" "yikou-light-food"
sha256sum "$TARBALL" > "$TARBALL.sha256"
echo "Build complete: $TARBALL"
