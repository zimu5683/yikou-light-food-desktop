"""Update startup health marker.

The updater writes a unique marker path/token before launching the new version.
The GUI writes the marker after the webview event loop starts.  A detached
replacement script can therefore distinguish "binary replaced" from "GUI
actually started", and roll back when startup fails.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

HEALTH_FILE_ENV = "YIKOU_UPDATE_HEALTH_FILE"
HEALTH_TOKEN_ENV = "YIKOU_UPDATE_HEALTH_TOKEN"


def begin_update_health_check(directory: str | Path) -> tuple[str, str]:
    """Create a unique token and marker path under ``directory``."""
    folder = Path(directory)
    token = secrets.token_hex(16)
    marker = folder / f".yikou-update-health-{os.getpid()}-{token[:8]}.json"
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass
    return token, str(marker)


def _atomic_write_json(path: Path, payload: dict) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def mark_startup_healthy(version: str = "") -> None:
    """Write the startup marker when this process was launched by the updater."""
    marker = os.environ.get(HEALTH_FILE_ENV, "").strip()
    if not marker:
        return
    token = os.environ.get(HEALTH_TOKEN_ENV, "").strip()
    try:
        path = Path(marker)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(path, {
            "token": token,
            "version": version,
            "pid": os.getpid(),
            "timestamp": time.time(),
        })
    except OSError:
        # 健康标记是更新器的辅助机制，写失败不应影响主程序启动。
        pass


def wait_for_health(marker: str | Path, token: str, *, timeout: float = 60.0,
                    interval: float = 0.25) -> bool:
    """Wait until the new process writes a marker containing ``token``."""
    path = Path(marker)
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and str(payload.get("token") or "") == token:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.01, float(interval)))


def clear_update_health(marker: str | Path) -> None:
    try:
        Path(marker).unlink(missing_ok=True)
    except OSError:
        pass
