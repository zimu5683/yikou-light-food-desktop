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

# WPS 云文档同步依赖金山官方 CLI（无 PyPI 包），按当前平台下载并用官方
# checksums.txt 校验 sha256 后放进 vendor/kdocs-cli/，由 spec 打进产物。
"$PYTHON_BIN" scripts/fetch_kdocs_cli.py

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

# 内置 Chromium 单独抓取到 vendor/browser/，与可执行文件同级打进 tar.gz；
# PLAYWRIGHT_BROWSERS_PATH=0 让 PyInstaller 的 Playwright hook 不要把本机
# 浏览器缓存也塞进单文件 exe。
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

# 内置浏览器：抓取并精简 Chromium，然后与二进制平铺打进同一个 tar.gz。
"$PYTHON_BIN" scripts/fetch_browser.py

rm -rf "$ROOT/dist/browser"
cp -a "$ROOT/vendor/browser" "$ROOT/dist/browser"

# Relative names keep the checksum file usable after download, wherever the
# user extracts it; an absolute path would leak the CI workspace location.
NAME="yikou-light-food-linux-${ARCH_TAG}.tar.gz"
tar -czf "$NAME" -C "$ROOT/dist" "yikou-light-food" "browser"
sha256sum "$NAME" > "$NAME.sha256"
echo "Build complete: ${ROOT}/${NAME}（含内置 Chromium）"
