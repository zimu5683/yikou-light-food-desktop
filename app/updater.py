"""GitHub Releases based update checker.

The checker is intentionally small and dependency-free so it also works from
the PyInstaller executable. Network failures are reported to the caller and
never prevent the main application from starting.
"""
from __future__ import annotations

import json
import re
import os
import base64
import hashlib
import subprocess
import sys
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
            if name == "yikou-light-food.exe":
                return asset
        return None

    @property
    def checksum_asset(self) -> dict[str, Any] | None:
        for asset in self.assets:
            if str(asset.get("name") or "").lower() == "yikou-light-food.exe.sha256":
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
    progress_callback: Callable[[int, int | None], None] | None = None,
) -> Path:
    """Download a release exe and schedule replacement after this process exits.

    Windows locks the running executable, so a short-lived command script does
    the final move and relaunches the updated file after the GUI closes.
    """
    if os.name != "nt":
        raise UpdateError("Automatic installation is currently supported on Windows only")
    if current_executable is None and not getattr(sys, "frozen", False):
        # In source mode sys.executable is python.exe.  Replacing it would
        # corrupt the user's Python installation.
        raise UpdateError("源码运行模式不支持自动安装，请前往 GitHub Release 页面下载")
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
            content_length = None
            headers = getattr(response, "headers", None)
            if headers is not None:
                try:
                    content_length = int(headers.get("Content-Length") or 0) or None
                except (TypeError, ValueError):
                    content_length = None
            downloaded = 0
            if progress_callback:
                progress_callback(downloaded, content_length)
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, content_length)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"Unable to download update: {exc}") from exc
    if temporary.stat().st_size < 1_000_000:
        temporary.unlink(missing_ok=True)
        raise UpdateError("Downloaded update is unexpectedly small")
    with temporary.open("rb") as downloaded_file:
        if downloaded_file.read(2) != b"MZ":
            temporary.unlink(missing_ok=True)
            raise UpdateError("Downloaded file is not a valid Windows executable")
    checksum_asset = release.checksum_asset
    checksum_url = str(checksum_asset.get("browser_download_url") if checksum_asset else "")
    checksum_parsed = urlparse(checksum_url)
    if checksum_parsed.scheme != "https" or checksum_parsed.hostname not in {"github.com", "objects.githubusercontent.com"}:
        temporary.unlink(missing_ok=True)
        raise UpdateError("Release does not contain a trusted SHA-256 checksum")
    try:
        checksum_request = Request(checksum_url, headers={"User-Agent": "yikou-light-food"})
        with (opener or urlopen)(checksum_request, timeout=timeout) as response:
            expected_hash = response.read().decode("ascii").strip().split()[0].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("invalid SHA-256")
        actual_hash = hashlib.sha256(temporary.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise UpdateError("Downloaded update failed SHA-256 verification")
    except UpdateError:
        temporary.unlink(missing_ok=True)
        raise
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, ValueError, IndexError) as exc:
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"Unable to verify update checksum: {exc}") from exc

    # Use an encoded PowerShell command rather than a .cmd file.  This keeps
    # paths containing Chinese characters intact and waits for the locked
    # PyInstaller executable to exit before replacing it.
    def quote(value: Any) -> str:
        return str(value).replace("'", "''")
    powershell = (
        "$ErrorActionPreference = 'Stop'\n"
        f"Wait-Process -Id {os.getpid()} -Timeout 120 -ErrorAction SilentlyContinue\n"
        f"Move-Item -LiteralPath '{quote(temporary)}' -Destination '{quote(target)}' -Force\n"
        f"Start-Process -FilePath '{quote(target)}'\n"
    )
    encoded_command = base64.b64encode(powershell.encode("utf-16le")).decode("ascii")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded_command],
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"Unable to start update installer: {exc}") from exc
    return target
