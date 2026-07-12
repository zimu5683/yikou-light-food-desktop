"""GitHub Releases based update checker.

The checker is intentionally small and dependency-free so it also works from
the PyInstaller executable. Network failures are reported to the caller and
never prevent the main application from starting.
"""
from __future__ import annotations

import json
import re
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import __version__

REPOSITORY = "zimu5683/yikou-light-food-desktop"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"


class UpdateError(RuntimeError):
    """Raised when the release endpoint cannot be queried or decoded."""


@dataclass(frozen=True)
class ReleaseInfo:
    tag_name: str
    name: str
    body: str
    html_url: str
    assets: tuple[dict[str, Any], ...] = ()

    @property
    def version(self) -> str:
        return normalize_version(self.tag_name)

    @property
    def executable_asset(self) -> dict[str, Any] | None:
        """Return the Windows executable asset attached to this release."""
        for asset in self.assets:
            name = str(asset.get("name") or "").lower()
            if name == "yikou-light-food.exe" or name.endswith(".exe"):
                return asset
        return None


def normalize_version(value: str) -> str:
    """Return a comparable version string (``v1.2.3`` -> ``1.2.3``)."""
    return str(value or "0").strip().lstrip("vV")


def _version_parts(value: str) -> tuple[tuple[int, ...], tuple[str, ...]]:
    value = normalize_version(value)
    # Ignore build metadata; compare prerelease identifiers according to the
    # SemVer rule where a release is newer than its prerelease.
    core, _, prerelease = value.partition("-")
    numbers = tuple(int(part) if part.isdigit() else 0 for part in core.split("."))
    pre = tuple(part for part in re.split(r"[.-]", prerelease) if part) if prerelease else ()
    return numbers, pre


def compare_versions(left: str, right: str) -> int:
    """Compare two versions, returning ``-1``, ``0`` or ``1``."""
    l_num, l_pre = _version_parts(left)
    r_num, r_pre = _version_parts(right)
    width = max(len(l_num), len(r_num))
    l_num += (0,) * (width - len(l_num))
    r_num += (0,) * (width - len(r_num))
    if l_num != r_num:
        return 1 if l_num > r_num else -1
    if not l_pre and not r_pre:
        return 0
    if not l_pre:
        return 1
    if not r_pre:
        return -1
    for left_part, right_part in zip(l_pre, r_pre):
        if left_part == right_part:
            continue
        if left_part.isdigit() and right_part.isdigit():
            return 1 if int(left_part) > int(right_part) else -1
        if left_part.isdigit() != right_part.isdigit():
            return -1 if left_part.isdigit() else 1
        return 1 if left_part > right_part else -1
    return (len(l_pre) > len(r_pre)) - (len(l_pre) < len(r_pre))


def _decode_release(payload: Any) -> ReleaseInfo:
    if not isinstance(payload, dict) or not payload.get("tag_name"):
        raise UpdateError("GitHub release response is missing tag_name")
    assets = payload.get("assets") or []
    if not isinstance(assets, list):
        assets = []
    return ReleaseInfo(
        tag_name=str(payload["tag_name"]),
        name=str(payload.get("name") or payload["tag_name"]),
        body=str(payload.get("body") or "").strip(),
        html_url=str(payload.get("html_url") or ""),
        assets=tuple(item for item in assets if isinstance(item, dict)),
    )


def check_for_update(
    current_version: str = __version__,
    *,
    timeout: float = 5.0,
    opener: Callable[..., Any] | None = None,
) -> ReleaseInfo | None:
    """Fetch the latest GitHub release and return it when it is newer."""
    request = Request(RELEASES_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "yikou-light-food"})
    open_func = opener or urlopen
    try:
        with open_func(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise UpdateError(f"Unable to check for updates: {exc}") from exc
    release = _decode_release(payload)
    return release if compare_versions(release.version, current_version) > 0 else None


def download_and_install(
    release: ReleaseInfo,
    *,
    current_executable: str | os.PathLike[str] | None = None,
    timeout: float = 60.0,
    opener: Callable[..., Any] | None = None,
) -> Path:
    """Download a release exe and schedule replacement after this process exits.

    Windows locks the running executable, so a short-lived command script does
    the final move and relaunches the updated file after the GUI closes.
    """
    if os.name != "nt":
        raise UpdateError("Automatic installation is currently supported on Windows only")
    asset = release.executable_asset
    url = str(asset.get("browser_download_url") if asset else "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "objects.githubusercontent.com"}:
        raise UpdateError("Release does not contain a trusted Windows executable download")
    target = Path(current_executable or sys.executable).resolve()
    if target.suffix.lower() != ".exe":
        raise UpdateError("Automatic installation is only available from the packaged exe")
    temporary = target.with_name(f".{target.stem}.update-{os.getpid()}.tmp")
    request = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "yikou-light-food"})
    try:
        with (opener or urlopen)(request, timeout=timeout) as response, temporary.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"Unable to download update: {exc}") from exc
    if temporary.stat().st_size < 1_000_000:
        temporary.unlink(missing_ok=True)
        raise UpdateError("Downloaded update is unexpectedly small")

    script = Path(tempfile.gettempdir()) / f"yikou-light-food-update-{os.getpid()}.cmd"
    script.write_text(
        "@echo off\r\n"
        "timeout /t 2 /nobreak >nul\r\n"
        f'move /Y "{temporary}" "{target}" >nul\r\n'
        f'if errorlevel 1 exit /b 1\r\nstart "" "{target}"\r\n'
        "del \"%~f0\"\r\n",
        encoding="ascii",
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(["cmd.exe", "/c", str(script)], creationflags=flags, close_fds=True)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        script.unlink(missing_ok=True)
        raise UpdateError(f"Unable to start update installer: {exc}") from exc
    return target
