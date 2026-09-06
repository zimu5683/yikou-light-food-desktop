"""Build the Vite frontend before a native package is frozen."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"


def main() -> None:
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise SystemExit(
            "pnpm is required to build the frontend. Install pnpm 11 or run "
            "'cd frontend && pnpm install && pnpm build' first."
        )
    subprocess.run([pnpm, "install", "--frozen-lockfile"], cwd=FRONTEND, check=True)
    subprocess.run([pnpm, "build"], cwd=FRONTEND, check=True)


if __name__ == "__main__":
    main()
