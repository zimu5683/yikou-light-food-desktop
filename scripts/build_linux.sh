#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Prefer the repository virtualenv for local builds. GitHub Actions provides
# an isolated setup-python interpreter, so its python3 remains the fallback.
if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -r requirements.txt
"$PYTHON_BIN" scripts/build_frontend.py

# pywebview's Linux backend uses the system GTK/WebKitGTK libraries. Fail
# before PyInstaller if the build host cannot import that backend.
if ! "$PYTHON_BIN" - <<'PY'
import gi

gi.require_version("Gtk", "3.0")
try:
    gi.require_version("WebKit2", "4.1")
except ValueError:
    gi.require_version("WebKit2", "4.0")

from gi.repository import Gtk, WebKit2  # noqa: F401
print("GTK/WebKitGTK runtime: OK")
PY
then
  echo "Unable to import GTK/WebKitGTK for pywebview." >&2
  echo "Install python3-gi, gir1.2-gtk-3.0, and gir1.2-webkit2-4.0/4.1 before building." >&2
  exit 1
fi

# Keep browser installation outside the bundle.  The application can
# download Chromium on first run when Edge/Chromium is unavailable.
export PLAYWRIGHT_BROWSERS_PATH=0
"$PYTHON_BIN" -m PyInstaller --clean --noconfirm "yikou-light-food.spec"

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
