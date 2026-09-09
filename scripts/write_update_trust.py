"""Write ``app/update_trust.json`` from release-time configuration.

Windows Authenticode publisher / macOS Team ID are public trust anchors, not
secrets.  They are written into the package during release so end users do not
need to set environment variables on their machines.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "app" / "update_trust.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-windows", action="store_true")
    parser.add_argument("--require-macos", action="store_true")
    args = parser.parse_args()

    windows = (
        os.environ.get("WINDOWS_AUTHENTICODE_PUBLISHER")
        or os.environ.get("YIKOU_WINDOWS_AUTHENTICODE_PUBLISHER")
        or ""
    ).strip()
    macos = (
        os.environ.get("MACOS_TEAM_ID")
        or os.environ.get("YIKOU_MACOS_TEAM_ID")
        or ""
    ).strip()
    if args.require_windows and not windows:
        print("缺少 Windows Authenticode 发布者配置，拒绝构建可自动更新的 Windows 包", file=sys.stderr)
        return 2
    if args.require_macos and not macos:
        print("缺少 macOS Team ID 配置，拒绝构建可自动更新的 macOS 包", file=sys.stderr)
        return 2
    if not windows:
        print("WARNING: 未配置 Windows Authenticode 发布者，本次构建的 Windows 自动更新将 fail-closed",
              file=sys.stderr)
    if not macos:
        print("WARNING: 未配置 macOS Team ID，本次构建的 macOS 自动更新将 fail-closed",
              file=sys.stderr)
    TARGET.write_text(
        json.dumps({
            "windows_authenticode_publisher": windows,
            "macos_team_id": macos,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
