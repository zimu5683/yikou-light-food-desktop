"""Application entry point."""
from __future__ import annotations

import sys


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
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, str(exc), "一口轻食更新失败", 0x10)
            except Exception:
                pass
            raise SystemExit(1)
        return
    if "--install-browser" in sys.argv:
        from .automation import ensure_browser
        print(ensure_browser("auto"))
        return
    from .gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
