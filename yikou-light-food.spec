# PyInstaller build specification for the desktop application.
# Build with: python -m PyInstaller --clean --noconfirm yikou-light-food.spec
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all

project = Path(SPECPATH)
datas, binaries, hiddenimports = collect_all("playwright")
# The browser payload is machine-specific and is intentionally not bundled.
datas = [item for item in datas if ".local-browsers" not in str(item[0])]
binaries = [item for item in binaries if ".local-browsers" not in str(item[0])]

analysis = Analysis(
    [str(project / "run.py")],
    pathex=[str(project)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # openpyxl treats numpy and lxml as optional accelerators and falls back to
    # the standard library when they are absent; excluding them (and the
    # bs4/yaml/soupsieve chain they pull in) keeps the executable lean.
    excludes=["numpy", "lxml", "bs4", "yaml", "soupsieve"],
    noarchive=False,
)
# Playwright's own PyInstaller hook re-collects the bundled browser payload
# (playwright/driver/package/.local-browsers) regardless of the filter above,
# so strip it after analysis too.  Only the Node driver is kept; the browser
# binary is resolved at runtime from a system Edge/Chrome or the Playwright
# Chromium cache, never from inside the executable.
for _toc in ("datas", "binaries"):
    setattr(analysis, _toc, [
        entry for entry in getattr(analysis, _toc)
        if ".local-browsers" not in str(entry[0]) and ".local-browsers" not in str(entry[1])
    ])
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="yikou-light-food",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX-packed PyInstaller bootloaders are frequently classified as
    # heuristic malware by Windows Defender and browser download filters.
    # Keep the portable one-file executable uncompressed; this is larger but
    # materially safer for end users and can still be Authenticode-signed in
    # the release workflow when a certificate is configured.
    upx=False,
    console=False,
    manifest=str(project / "windows-app.manifest"),
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="yikou-light-food.app",
        bundle_identifier="com.zimu5683.yikou-light-food",
    )

