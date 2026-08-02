"""GitHub Releases based update checker.

The checker is intentionally small and dependency-free so it also works from
the PyInstaller executable. Network failures are reported to the caller and
never prevent the main application from starting.
"""
from __future__ import annotations

import json
import re
import os
import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import __version__

REPOSITORY = "zimu5683/yikou-light-food-desktop"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
LATEST_MANIFEST_URL = f"https://github.com/{REPOSITORY}/releases/latest/download/latest.json"
TRUSTED_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
MAX_UPDATE_SIZE = 512 * 1024 * 1024


class UpdateError(RuntimeError):
    """Raised when the release endpoint cannot be queried or decoded."""


@dataclass(frozen=True)
class ReleaseInfo:
    tag_name: str
    name: str
    body: str
    html_url: str
    assets: tuple[dict[str, Any], ...] = ()
    manifest_source: str = "api"
    manifest_url: str = ""

    @property
    def version(self) -> str:
        return normalize_version(self.tag_name)

    @property
    def executable_asset(self) -> dict[str, Any] | None:
        """Return the Windows executable asset attached to this release."""
        for asset in self.assets:
            name = str(asset.get("name") or "").lower()
            if name == "yikou-light-food.exe" and safe_asset_name(name):
                return asset
        return None

    @property
    def checksum_asset(self) -> dict[str, Any] | None:
        for asset in self.assets:
            name = str(asset.get("name") or "").lower()
            if name == "yikou-light-food.exe.sha256" and safe_asset_name(name):
                return asset
        executable = self.executable_asset
        checksum_url = str((executable or {}).get("sha256_url") or "")
        if checksum_url:
            return {"name": "yikou-light-food.exe.sha256", "browser_download_url": checksum_url}
        return None

    @property
    def executable_size(self) -> int | None:
        asset = self.executable_asset
        try:
            size = int(asset.get("size")) if asset and asset.get("size") is not None else None
            return size if size and size > 0 else None
        except (TypeError, ValueError):
            return None


def safe_asset_name(name: str) -> bool:
    """Reject path traversal and unexpected release asset names."""
    value = str(name or "")
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        return False
    if ".." in value or any(ord(char) < 32 for char in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value))


def _trusted_url(value: str) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in TRUSTED_DOWNLOAD_HOSTS


def _asset_url(asset: dict[str, Any] | None) -> str:
    if not asset:
        return ""
    return str(asset.get("browser_download_url") or asset.get("download_url") or asset.get("url") or "")


def select_platform_assets(assets: list[dict[str, Any]] | tuple[dict[str, Any], ...],
                           *, platform: str | None = None, architecture: str | None = None) -> tuple[dict[str, Any], ...]:
    """Filter release assets by platform/architecture and safe file names."""
    platform = (platform or ("windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux")).lower()
    architecture = (architecture or ("arm64" if (platform == "macos" and sys.platform == "darwin" and os.uname().machine.lower() in {"arm64", "aarch64"}) else "x64")).lower()
    result: list[dict[str, Any]] = []
    for asset in assets:
        name = str(asset.get("name") or "")
        lower = name.lower()
        if not safe_asset_name(name):
            continue
        if platform == "windows" and lower.endswith(".exe") and "macos" not in lower and "linux" not in lower:
            result.append(asset)
        elif platform == "macos" and lower.endswith(".zip") and "macos" in lower and (architecture in lower or "arm64" not in lower and "x64" not in lower):
            result.append(asset)
        elif platform == "linux" and lower.endswith((".appimage", ".tar.gz", ".deb")) and "linux" in lower:
            result.append(asset)
    return tuple(result)


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
    normalized_assets = []
    for item in assets:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        if not copied.get("browser_download_url") and copied.get("url"):
            copied["browser_download_url"] = copied["url"]
        normalized_assets.append(copied)
    return ReleaseInfo(
        tag_name=str(payload["tag_name"]),
        name=str(payload.get("name") or payload["tag_name"]),
        body=str(payload.get("body") or "").strip(),
        html_url=str(payload.get("html_url") or ""),
        assets=tuple(normalized_assets),
        manifest_source="api",
    )


def _decode_manifest(payload: Any, *, source_url: str = LATEST_MANIFEST_URL) -> ReleaseInfo:
    if not isinstance(payload, dict):
        raise UpdateError("latest.json 格式无效")
    schema = payload.get("schema_version", 1)
    if str(schema) not in {"1", "1.0"}:
        raise UpdateError("latest.json schema_version 不受支持")
    version = str(payload.get("version") or payload.get("tag_name") or "").strip()
    if not version:
        raise UpdateError("latest.json 缺少 version")
    assets: list[dict[str, Any]] = []
    raw_assets = payload.get("assets") or []
    if not isinstance(raw_assets, list):
        raise UpdateError("latest.json assets 格式无效")
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not safe_asset_name(name):
            continue
        copied = dict(item)
        if item.get("url"):
            copied["browser_download_url"] = item["url"]
        if item.get("sha256_url"):
            copied["sha256_url"] = item["sha256_url"]
        assets.append(copied)
    return ReleaseInfo(
        tag_name=version if version.lower().startswith("v") else f"v{version}",
        name=str(payload.get("name") or version),
        body=str(payload.get("body") or payload.get("release_summary") or payload.get("notes") or "").strip(),
        html_url=str(payload.get("url") or payload.get("html_url") or ""),
        assets=tuple(assets),
        manifest_source="manifest",
        manifest_url=source_url,
    )


def _fetch_json(url: str, *, timeout: float, opener: Callable[..., Any]) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "yikou-light-food"})
    with opener(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def check_for_update(
    current_version: str = __version__,
    *,
    timeout: float = 5.0,
    opener: Callable[..., Any] | None = None,
) -> ReleaseInfo | None:
    """Fetch the latest GitHub release and return it when it is newer."""
    open_func = opener or urlopen
    errors: list[str] = []
    release: ReleaseInfo | None = None
    try:
        release = _decode_manifest(_fetch_json(LATEST_MANIFEST_URL, timeout=timeout, opener=open_func))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, UpdateError) as exc:
        errors.append(f"manifest: {exc}")
    if release is None:
        try:
            release = _decode_release(_fetch_json(RELEASES_URL, timeout=timeout, opener=open_func))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, UpdateError) as exc:
            errors.append(f"api: {exc}")
            raise UpdateError("Unable to check for updates: " + "; ".join(errors)) from exc
    return release if compare_versions(release.version, current_version) > 0 else None


def download_and_install(
    release: ReleaseInfo,
    *,
    current_executable: str | os.PathLike[str] | None = None,
    timeout: float = 60.0,
    opener: Callable[..., Any] | None = None,
    progress_callback: Callable[[int, int | None], None] | None = None,
    stage_callback: Callable[[str], None] | None = None,
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
    asset_name = str(asset.get("name") if asset else "")
    url = _asset_url(asset)
    if not safe_asset_name(asset_name) or asset_name.lower() != "yikou-light-food.exe" or not _trusted_url(url):
        raise UpdateError("Release does not contain a trusted Windows executable download")
    target = Path(current_executable or sys.executable).resolve()
    if target.suffix.lower() != ".exe":
        raise UpdateError("Automatic installation is only available from the packaged exe")
    if not target.parent.exists() or not os.access(target.parent, os.W_OK):
        raise UpdateError("安装目录不可写，请将程序移动到可写目录后重试")
    expected_size = release.executable_size
    if expected_size and expected_size > MAX_UPDATE_SIZE:
        raise UpdateError("更新资源大小异常")
    try:
        required_space = (expected_size or 1_000_000) + 1_048_576
        if shutil.disk_usage(target.parent).free < required_space:
            raise UpdateError("磁盘剩余空间不足，无法安装更新")
    except OSError as exc:
        raise UpdateError(f"无法检查磁盘空间：{exc}") from exc
    temporary = target.with_name(f".{target.stem}.update-{os.getpid()}.tmp")
    request = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "yikou-light-food"})
    if stage_callback:
        stage_callback("下载更新")
    try:
        with (opener or urlopen)(request, timeout=timeout) as response, temporary.open("wb") as output:
            content_length = None
            headers = getattr(response, "headers", None)
            if headers is not None:
                try:
                    content_length = int(headers.get("Content-Length") or 0) or None
                except (TypeError, ValueError):
                    content_length = None
            if content_length and content_length > MAX_UPDATE_SIZE:
                raise UpdateError("更新资源大小异常")
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
            if expected_size and downloaded != expected_size:
                raise UpdateError("下载文件大小与清单不一致")
            if downloaded > MAX_UPDATE_SIZE:
                raise UpdateError("更新资源大小异常")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, UpdateError) as exc:
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
    checksum_url = str((checksum_asset or {}).get("sha256_url") or _asset_url(checksum_asset))
    if not _trusted_url(checksum_url):
        temporary.unlink(missing_ok=True)
        raise UpdateError("Release does not contain a trusted SHA-256 checksum")
    try:
        if stage_callback:
            stage_callback("校验安装包")
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

    # A running executable cannot replace itself on Windows.  Launch a copy
    # of the freshly downloaded version from the user's temporary directory;
    # that copy waits for this process to exit, atomically replaces the old
    # executable, and starts the installed copy.  This avoids PowerShell and
    # works with Chinese paths.
    helper = Path(tempfile.gettempdir()) / f"yikou-light-food-updater-{os.getpid()}.exe"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        if stage_callback:
            stage_callback("准备替换")
        shutil.copy2(temporary, helper)
        subprocess.Popen(
            [str(helper), "--apply-update", str(temporary), str(target)],
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        helper.unlink(missing_ok=True)
        raise UpdateError(f"Unable to start update installer: {exc}") from exc
    return target


def apply_pending_update(
    source: str | os.PathLike[str],
    target: str | os.PathLike[str],
    *,
    timeout: float = 120.0,
    retry_interval: float = 0.5,
    launcher: Callable[..., Any] = subprocess.Popen,
) -> Path:
    """Replace ``target`` with a verified downloaded exe and restart it.

    This runs inside the temporary updater copy, not inside the installed
    executable.  Retrying ``os.replace`` is both a lock check and an atomic
    replacement once the original GUI process has fully exited.
    """
    source_path = Path(source).resolve()
    target_path = Path(target).resolve()
    if not source_path.is_file() or target_path.suffix.lower() != ".exe":
        raise UpdateError("Pending update files are invalid")
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            os.replace(source_path, target_path)
            last_error = None
            break
        except OSError as exc:
            last_error = exc
            time.sleep(retry_interval)
    if last_error is not None:
        raise UpdateError(f"Unable to replace the running executable: {last_error}") from last_error
    try:
        launcher(
            [str(target_path)],
            cwd=str(target_path.parent),
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise UpdateError(f"Update installed but the application could not restart: {exc}") from exc
    _schedule_helper_cleanup(Path(sys.executable).resolve())
    return target_path


def _schedule_helper_cleanup(helper: Path) -> None:
    """Delete the temporary updater after its process has exited."""
    if helper.name.lower().startswith("yikou-light-food-updater-"):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        command = f'ping 127.0.0.1 -n 3 >nul & del /f /q "{helper}"'
        try:
            subprocess.Popen(
                ["cmd.exe", "/d", "/c", command],
                creationflags=flags,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            # The helper lives in the system temp directory; a failed cleanup
            # does not affect the installed application or future updates.
            pass
