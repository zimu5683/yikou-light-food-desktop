# PyInstaller build specification for the desktop application.
# Build with: python -m PyInstaller --clean --noconfirm yikou-light-food.spec
from pathlib import Path

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
    excludes=[],
    noarchive=False,
)
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
    upx=True,
    console=False,
)

