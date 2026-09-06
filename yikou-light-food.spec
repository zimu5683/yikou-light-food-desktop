# PyInstaller build specification for the desktop application.
# Build with: python -m PyInstaller --clean --noconfirm yikou-light-food.spec
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all

project = Path(SPECPATH)
frontend_dist = project / "frontend" / "dist"
if not (frontend_dist / "index.html").is_file():
    raise SystemExit(
        "Missing frontend/dist/index.html. Build the frontend first with "
        "'cd frontend && pnpm install && pnpm build'."
    )

datas, binaries, hiddenimports = collect_all("playwright")
# Include the complete Vite output. The HTML references sibling assets such as
# the favicon and icon sprite even though its JS/CSS are inlined. Build the
# regular two-item ``(source, destination)`` data tuples expected by Analysis.
datas += [
    (str(path), str(Path("frontend") / path.relative_to(frontend_dist).parent))
    for path in frontend_dist.rglob("*")
    if path.is_file()
]
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

# Linux 版本依赖系统 GTK/WebKitGTK/GLib。PyInstaller 会把构建机上的整套
# GTK/Glib/C++ 运行库也收进单文件包里；当用户发行版比构建机新时，包内旧版
# libstdc++/libglib/libmount 等会遮蔽系统新库，导致 WebKitGTK 初始化失败
# （例如 CXXABI_1.3.15 not found）。这里在 Linux 上把这些系统库从包中去掉，
# 让程序运行时始终使用当前系统的 GTK/WebKitGTK/GLib，而不是捆绑的旧副本。
if sys.platform.startswith("linux"):
    _LINUX_SYSTEM_LIB_NAMES = {
        # C/C++ runtime
        "libstdc++.so.6",
        "libgcc_s.so.1",
        # GLib/GObject/GIO/ introspection
        "libglib-2.0.so.0",
        "libgobject-2.0.so.0",
        "libgio-2.0.so.0",
        "libgmodule-2.0.so.0",
        "libgirepository-1.0.so.1",
        "libmount.so.1",
        "libblkid.so.1",
        "libuuid.so.1",
        "libselinux.so.1",
        # GTK 3 / GDK / Pango / Cairo stack
        "libgtk-3.so.0",
        "libgdk-3.so.0",
        "libgdk_pixbuf-2.0.so.0",
        "libatk-1.0.so.0",
        "libatk-bridge-2.0.so.0",
        "libatspi.so.0",
        "libcairo.so.2",
        "libcairo-gobject.so.2",
        "libpango-1.0.so.0",
        "libpangocairo-1.0.so.0",
        "libpangoft2-1.0.so.0",
        "libepoxy.so.0",
        "libharfbuzz.so.0",
        "libfontconfig.so.1",
        "libfreetype.so.6",
        "libfribidi.so.0",
        "libpixman-1.so.0",
        "libpng16.so.16",
        "libjpeg.so.8",
        "libtiff.so.5",
        "libwebp.so.7",
        "librsvg-2.so.2",
        # ICU: 避免包内 Ubuntu 22.04 的 ICU 70 遮蔽新版系统 ICU
        "libicudata.so.70",
        "libicuuc.so.70",
        # 其它 GLib/GTK 常见依赖
        "libpcre.so.3",
        "libpcre2-8.so.0",
        "libffi.so.8",
        "libxml2.so.2",
    }
    _LINUX_SYSTEM_LIB_DIR_PREFIXES = (
        "gio_modules/",
        "lib/gdk-pixbuf/",
    )

    def _use_system_linux_lib(entry: tuple) -> bool:
        dest = str(entry[0])
        name = Path(dest).name
        if dest.startswith(_LINUX_SYSTEM_LIB_DIR_PREFIXES):
            return False
        if name in _LINUX_SYSTEM_LIB_NAMES:
            return False
        return True

    analysis.binaries = [
        entry for entry in analysis.binaries if _use_system_linux_lib(entry)
    ]
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
