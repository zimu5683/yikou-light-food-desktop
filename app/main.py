"""Application entry point."""
from __future__ import annotations

import sys

from .automation import ensure_browser


def main() -> None:
    if "--install-browser" in sys.argv:
        print(ensure_browser("auto"))
        return
    from .gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
