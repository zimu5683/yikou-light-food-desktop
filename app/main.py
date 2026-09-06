"""Application entry point."""
from __future__ import annotations

import ctypes
import os
import sys


def _enable_high_dpi_awareness() -> str:
    """Enable per-monitor DPI awareness before a window is created (Windows)."""
    if os.name != "nt":
        return "unsupported"
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except (AttributeError, OSError, TypeError):
        pass
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE == 2.
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except (AttributeError, OSError, TypeError):
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError, TypeError):
        pass
    return "unavailable"


def main() -> None:
    if "--apply-update" in sys.argv:
        index = sys.argv.index("--apply-update")
        if len(sys.argv) < index + 3:
            raise SystemExit(2)
        from .updater import UpdateError, apply_pending_update
        try:
            apply_pending_update(sys.argv[index + 1], sys.argv[index + 2])
        except UpdateError as exc:
            # The updater copy has no console; show a native error dialog so a
            # replacement failure is no longer silent.
            try:
                ctypes.windll.user32.MessageBoxW(None, str(exc), "一口轻食更新失败", 0x10)
            except Exception:
                pass
            raise SystemExit(1)
        return
    if "--install-browser" in sys.argv:
        from .automation import ensure_browser
        print(ensure_browser("auto"))
        return
    if "--version" in sys.argv:
        from . import __version__
        print(f"yikou-light-food {__version__}")
        return
    _enable_high_dpi_awareness()
    from .webview_app import run as webview_run
    webview_run()


if __name__ == "__main__":
    main()
