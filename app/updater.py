"""GitHub Releases based update checker.

The checker is intentionally small and dependency-free so it also works from
the PyInstaller executable. Network failures are reported to the caller and
never prevent the main application from starting.
"""
from __future__ import annotations

import json
import re
import os
import shlex
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from typing import Any, Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import __version__
from . import bspatch

REPOSITORY = "zimu5683/yikou-light-food-desktop"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
LATEST_MANIFEST_URL = f"https://github.com/{REPOSITORY}/releases/latest/download/latest.json"
# GitHub 直连在国内经常不可达，检查更新与下载都依次尝试：直连 → 国内加速镜像。
# 前缀只到镜像域名，候选 = 前缀 + 完整 GitHub URL（ghproxy 类服务要求保持原路径）。
GITHUB_MIRROR_PREFIXES = (
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
    "https://ghfast.top/",
)
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
    patches: tuple[dict[str, Any], ...] = ()
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

    @property
    def macos_asset(self) -> dict[str, Any] | None:
        """Return the macOS ``.app`` archive attached to this release."""
        for asset in self.assets:
            name = str(asset.get("name") or "").lower()
            if name == "yikou-light-food-macos.zip" and safe_asset_name(name):
                return asset
        return None

    @property
    def macos_checksum_asset(self) -> dict[str, Any] | None:
        for asset in self.assets:
            name = str(asset.get("name") or "").lower()
            if name == "yikou-light-food-macos.zip.sha256" and safe_asset_name(name):
                return asset
        archive = self.macos_asset
        checksum_url = str((archive or {}).get("sha256_url") or "")
        if checksum_url:
            return {"name": "yikou-light-food-macos.zip.sha256", "browser_download_url": checksum_url}
        return None

    @property
    def linux_asset(self) -> dict[str, Any] | None:
        """Return the Linux archive attached to this release."""
        for asset in self.assets:
            name = str(asset.get("name") or "").lower()
            if name == "yikou-light-food-linux-x64.tar.gz" and safe_asset_name(name):
                return asset
        return None

    @property
    def linux_checksum_asset(self) -> dict[str, Any] | None:
        for asset in self.assets:
            name = str(asset.get("name") or "").lower()
            if name == "yikou-light-food-linux-x64.tar.gz.sha256" and safe_asset_name(name):
                return asset
        archive = self.linux_asset
        checksum_url = str((archive or {}).get("sha256_url") or "")
        if checksum_url:
            return {"name": "yikou-light-food-linux-x64.tar.gz.sha256", "browser_download_url": checksum_url}
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


def _github_url_candidates(url: str) -> list[str]:
    """返回 [直连, 镜像1, 镜像2...]，镜像只对 github.com 的 URL 生效。"""
    if url.startswith("https://github.com/"):
        return [url] + [f"{prefix}{url}" for prefix in GITHUB_MIRROR_PREFIXES]
    return [url]


def _asset_url(asset: dict[str, Any] | None) -> str:
    if not asset:
        return ""
    return str(asset.get("browser_download_url") or asset.get("download_url") or asset.get("url") or "")


def _embedded_asset_sha256(asset: dict[str, Any] | None) -> str:
    """Return the SHA-256 embedded in a latest.json asset entry, when valid."""
    value = str((asset or {}).get("sha256") or "").strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else ""


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
        patches=(),
        manifest_source="api",
    )


def _decode_patches(payload: Any) -> tuple[dict[str, Any], ...]:
    """解析 latest.json 里的差分补丁清单，过滤掉不完整或不受信任的条目。"""
    raw_patches = payload.get("patches") or []
    if not isinstance(raw_patches, list):
        return ()
    patches: list[dict[str, Any]] = []
    for item in raw_patches:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        url = str(item.get("url") or item.get("browser_download_url") or "")
        patch_sha = str(item.get("sha256") or "").strip().lower()
        from_sha = str(item.get("from_sha256") or "").strip().lower()
        target_sha = str(item.get("target_sha256") or "").strip().lower()
        if (
            safe_asset_name(name)
            and _trusted_url(url)
            and re.fullmatch(r"[0-9a-f]{64}", patch_sha)
            and re.fullmatch(r"[0-9a-f]{64}", from_sha)
            and re.fullmatch(r"[0-9a-f]{64}", target_sha)
        ):
            patches.append({
                "name": name,
                "url": url,
                "sha256": patch_sha,
                "from_sha256": from_sha,
                "target_sha256": target_sha,
            })
    return tuple(patches)


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
    patches = _decode_patches(payload)
    return ReleaseInfo(
        tag_name=version if version.lower().startswith("v") else f"v{version}",
        name=str(payload.get("name") or version),
        body=str(payload.get("body") or payload.get("release_summary") or payload.get("notes") or "").strip(),
        html_url=str(payload.get("url") or payload.get("html_url") or ""),
        assets=tuple(assets),
        patches=patches,
        manifest_source="manifest",
        manifest_url=source_url,
    )


def _fetch_json(url: str, *, timeout: float, opener: Callable[..., Any]) -> Any:
    """按 [直连, 镜像1, 镜像2...] 依次尝试；每个源内部重试 1 次，抵御网络瞬断。"""
    last_error: Exception | None = None
    for candidate in _github_url_candidates(url):
        for attempt in range(2):
            request = Request(candidate, headers={"Accept": "application/json", "User-Agent": "yikou-light-food"})
            try:
                with opener(request, timeout=timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.4)
    if len(_github_url_candidates(url)) == 1:
        # 没有镜像可回退时保持原始异常，与旧行为一致。
        assert last_error is not None
        raise last_error
    assert last_error is not None
    raise UpdateError(f"Unable to fetch update metadata: {last_error}") from last_error


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
    macOS replaces the whole ``.app`` bundle instead.  Linux ships a single
    PyInstaller onefile binary inside a tar.gz; a detached shell waits for
    this process to exit, renames the staged file over it and execs it.
    """
    if sys.platform == "darwin":
        return _download_and_install_macos(
            release,
            timeout=timeout,
            opener=opener,
            progress_callback=progress_callback,
            stage_callback=stage_callback,
        )
    if sys.platform.startswith("linux"):
        return _download_and_install_linux(
            release,
            timeout=timeout,
            opener=opener,
            progress_callback=progress_callback,
            stage_callback=stage_callback,
        )
    if os.name != "nt":
        raise UpdateError("Automatic installation is currently supported on Windows and macOS only")
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
    # 优先走差分更新：只下载很小的补丁，用本地 exe 还原出完整新版。
    patch = _find_applicable_patch(release, target)
    if patch is not None:
        return _download_and_apply_patch(
            release, patch, target,
            timeout=timeout, opener=opener,
            progress_callback=progress_callback, stage_callback=stage_callback,
        )
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
    if stage_callback:
        stage_callback("下载更新")
    last_error: Exception | None = None
    for candidate in _github_url_candidates(url):
        temporary.unlink(missing_ok=True)
        try:
            _stream_download(
                candidate,
                temporary,
                timeout=timeout,
                opener=opener or urlopen,
                expected_size=expected_size,
                progress_callback=progress_callback,
            )
            last_error = None
            break
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, UpdateError) as exc:
            last_error = exc
    if last_error is not None:
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"Unable to download update: {last_error}") from last_error
    if temporary.stat().st_size < 1_000_000:
        temporary.unlink(missing_ok=True)
        raise UpdateError("Downloaded update is unexpectedly small")
    with temporary.open("rb") as downloaded_file:
        if downloaded_file.read(2) != b"MZ":
            temporary.unlink(missing_ok=True)
            raise UpdateError("Downloaded file is not a valid Windows executable")
    checksum_asset = release.checksum_asset
    checksum_url = str((checksum_asset or {}).get("sha256_url") or _asset_url(checksum_asset))
    # latest.json 内嵌的官方哈希：GitHub 直连不可达（镜像存在的场景）时，
    # 校验文件同样拉不到，此时回退到清单内嵌的同一 SHA-256。发布工作流会把
    # 官方哈希同时写入校验文件与清单。镜像若被完全控制，此回退理论上可被
    # 绕过；彻底方案是为 exe 做代码签名。
    embedded_hash = _embedded_asset_sha256(release.executable_asset)
    try:
        if stage_callback:
            stage_callback("校验安装包")
        if _trusted_url(checksum_url):
            try:
                checksum_request = Request(checksum_url, headers={"User-Agent": "yikou-light-food"})
                with (opener or urlopen)(checksum_request, timeout=timeout) as response:
                    expected_hash = response.read().decode("ascii").strip().split()[0].lower()
            except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, ValueError, IndexError) as exc:
                expected_hash = embedded_hash
                if not expected_hash:
                    raise UpdateError(f"Unable to verify update checksum: {exc}") from exc
        else:
            expected_hash = embedded_hash
            if not expected_hash:
                raise UpdateError("Release does not contain a trusted SHA-256 checksum")
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
    return _schedule_windows_replacement(temporary, target, stage_callback)


def _sha256_file(path: Path) -> str:
    """流式计算文件的 SHA-256，避免一次性读入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _stream_download(
    url: str,
    destination: Path,
    *,
    timeout: float,
    opener: Callable[..., Any],
    expected_size: int | None,
    progress_callback: Callable[[int, int | None], None] | None,
) -> None:
    """把单个 URL 流式下载到 destination；瞬断重试 3 次（退避 0.5s/1s）。"""
    request = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "yikou-light-food"})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with opener(request, timeout=timeout) as response, destination.open("wb") as output:
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
            return
        except HTTPError:
            raise
        except (URLError, TimeoutError, OSError, ValueError, UpdateError) as exc:
            last_error = exc
            destination.unlink(missing_ok=True)
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _find_applicable_patch(release: ReleaseInfo, target: Path) -> dict[str, Any] | None:
    """按本地 exe 的实际 SHA-256 匹配补丁；不匹配则返回 None（回退全量下载）。"""
    if not release.patches:
        return None
    if not target.is_file():
        return None
    local_sha = _sha256_file(target)
    for patch in release.patches:
        if patch["from_sha256"] == local_sha:
            return patch
    return None


def _schedule_windows_replacement(
    temporary: Path,
    target: Path,
    stage_callback: Callable[[str], None] | None,
) -> Path:
    """把已验证的新 exe 交给临时 helper，等待本进程退出后原子替换并重启。"""
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


def _download_patch_file(
    patch: dict[str, Any],
    patch_file: Path,
    *,
    timeout: float,
    opener: Callable[..., Any] | None,
    progress_callback: Callable[[int, int | None], None] | None,
) -> None:
    """把补丁下载到 patch_file（多候选重试）；失败时清理并抛 UpdateError。"""
    last_error: Exception | None = None
    for candidate in _github_url_candidates(patch["url"]):
        patch_file.unlink(missing_ok=True)
        try:
            with (opener or urlopen)(Request(candidate, headers={"Accept": "application/octet-stream", "User-Agent": "yikou-light-food"}), timeout=timeout) as response, patch_file.open("wb") as output:
                received = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    received += len(chunk)
                    if received > MAX_UPDATE_SIZE:
                        raise UpdateError("补丁大小异常")
                    if progress_callback:
                        progress_callback(received, received)
            last_error = None
            break
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, UpdateError) as exc:
            last_error = exc
    if last_error is not None:
        patch_file.unlink(missing_ok=True)
        raise UpdateError(f"Unable to download patch: {last_error}") from last_error


def _download_and_apply_patch(
    release: ReleaseInfo,
    patch: dict[str, Any],
    target: Path,
    *,
    timeout: float,
    opener: Callable[..., Any] | None,
    progress_callback: Callable[[int, int | None], None] | None,
    stage_callback: Callable[[str], None] | None,
) -> Path:
    """下载差分补丁，用本地 exe 还原出完整新版，校验后替换并重启。"""
    patch_file = target.with_name(f".{target.stem}.patch-{os.getpid()}.tmp")
    temporary = target.with_name(f".{target.stem}.update-{os.getpid()}.tmp")
    try:
        if shutil.disk_usage(target.parent).free < target.stat().st_size + 1_048_576:
            raise UpdateError("磁盘剩余空间不足，无法安装更新")
    except OSError as exc:
        raise UpdateError(f"无法检查磁盘空间：{exc}") from exc

    if stage_callback:
        stage_callback("下载差分补丁")
    _download_patch_file(patch, patch_file, timeout=timeout, opener=opener, progress_callback=progress_callback)
    temporary.unlink(missing_ok=True)

    try:
        if _sha256_file(patch_file) != patch["sha256"]:
            raise UpdateError("补丁 SHA-256 校验失败")
        if stage_callback:
            stage_callback("应用差分补丁")
        bspatch.apply_file(target, patch_file, temporary)
    except (OSError, ValueError) as exc:
        patch_file.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"Unable to apply patch: {exc}") from exc
    finally:
        patch_file.unlink(missing_ok=True)

    try:
        if _sha256_file(temporary) != patch["target_sha256"]:
            raise UpdateError("还原后的文件 SHA-256 校验失败")
        with temporary.open("rb") as handle:
            if handle.read(2) != b"MZ":
                raise UpdateError("还原后的文件不是有效的 Windows 可执行文件")
    except UpdateError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"Unable to verify rebuilt file: {exc}") from exc

    return _schedule_windows_replacement(temporary, target, stage_callback)


def _download_and_apply_patch_linux(
    release: ReleaseInfo,
    patch: dict[str, Any],
    target: Path,
    *,
    timeout: float,
    opener: Callable[..., Any] | None,
    progress_callback: Callable[[int, int | None], None] | None,
    stage_callback: Callable[[str], None] | None,
) -> Path:
    """下载差分补丁，用本地二进制还原出完整新版，校验后替换并重启。

    补丁以「解压后的裸 onefile 二进制」为基线：用户本地正好有这个文件，
    其 SHA-256 应命中补丁的 ``from_sha256``（见 ``_find_applicable_patch``）。
    还原结果写入安装目录的 staging，退出后原子替换并重启（与全量更新一致）。
    """
    install_dir = target.parent
    workdir = Path(tempfile.mkdtemp(prefix="yikou-light-food-update-"))
    staging = install_dir / f".{target.stem}.update-{os.getpid()}"
    if staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
    elif staging.exists():
        staging.unlink(missing_ok=True)
    patch_file = workdir / "update.patch"
    rebuilt = staging / target.name
    try:
        try:
            # 还原结果与补丁都要占空间，按本地二进制大小的 2 倍预留。
            if shutil.disk_usage(install_dir).free < target.stat().st_size * 2 + 1_048_576:
                raise UpdateError("磁盘剩余空间不足，无法安装更新")
        except OSError as exc:
            raise UpdateError(f"无法检查磁盘空间：{exc}") from exc

        if stage_callback:
            stage_callback("下载差分补丁")
        _download_patch_file(patch, patch_file, timeout=timeout, opener=opener, progress_callback=progress_callback)
        staging.mkdir(parents=True, exist_ok=True)

        try:
            if _sha256_file(patch_file) != patch["sha256"]:
                raise UpdateError("补丁 SHA-256 校验失败")
            if stage_callback:
                stage_callback("应用差分补丁")
            bspatch.apply_file(target, patch_file, rebuilt)
        except (OSError, ValueError) as exc:
            raise UpdateError(f"Unable to apply patch: {exc}") from exc
        finally:
            patch_file.unlink(missing_ok=True)

        try:
            if _sha256_file(rebuilt) != patch["target_sha256"]:
                raise UpdateError("还原后的文件 SHA-256 校验失败")
            with rebuilt.open("rb") as handle:
                if handle.read(4) != b"\x7fELF":
                    raise UpdateError("还原后的文件不是有效的 Linux 可执行文件")
        except UpdateError:
            raise
        except OSError as exc:
            raise UpdateError(f"Unable to verify rebuilt file: {exc}") from exc
        rebuilt.chmod(0o755)

        return _schedule_linux_replacement(rebuilt, target, workdir, staging, stage_callback)
    except BaseException:
        patch_file.unlink(missing_ok=True)
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _download_and_install_macos(
    release: ReleaseInfo,
    *,
    timeout: float = 60.0,
    opener: Callable[..., Any] | None = None,
    progress_callback: Callable[[int, int | None], None] | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> Path:
    """Download the macOS archive, replace the running .app, and relaunch.

    A PyInstaller macOS build runs from inside a ``yikou-light-food.app``
    bundle.  macOS does not lock the bundle, but replacing it while the
    process is still reading its resources is unreliable, so a detached shell
    waits for this process to exit before swapping in the freshly extracted
    bundle and relaunching it with ``open``.
    """
    if not getattr(sys, "frozen", False):
        raise UpdateError("源码运行模式不支持自动安装，请前往 GitHub Release 页面下载")
    asset = release.macos_asset
    asset_name = str(asset.get("name") if asset else "")
    url = _asset_url(asset)
    if not safe_asset_name(asset_name) or not asset_name.lower().endswith(".zip") or not _trusted_url(url):
        raise UpdateError("Release does not contain a trusted macOS archive")
    executable = Path(sys.executable).resolve()
    if executable.parent.name != "MacOS" or executable.parent.parent.name != "Contents":
        raise UpdateError("无法确定当前应用包结构，无法自动更新")
    app_bundle = executable.parent.parent.parent
    if app_bundle.suffix.lower() != ".app":
        raise UpdateError("无法确定当前应用包路径")

    workdir = Path(tempfile.mkdtemp(prefix="yikou-light-food-update-"))
    archive = workdir / asset_name
    if stage_callback:
        stage_callback("下载更新")
    last_error: Exception | None = None
    for candidate in _github_url_candidates(url):
        archive.unlink(missing_ok=True)
        try:
            _stream_download(
                candidate,
                archive,
                timeout=timeout,
                opener=opener or urlopen,
                expected_size=None,
                progress_callback=progress_callback,
            )
            last_error = None
            break
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, UpdateError) as exc:
            last_error = exc
    if last_error is not None:
        shutil.rmtree(workdir, ignore_errors=True)
        raise UpdateError(f"Unable to download update: {last_error}") from last_error

    checksum_asset = release.macos_checksum_asset
    checksum_url = str((checksum_asset or {}).get("sha256_url") or _asset_url(checksum_asset))
    embedded_hash = _embedded_asset_sha256(release.macos_asset)
    try:
        if stage_callback:
            stage_callback("校验安装包")
        if _trusted_url(checksum_url):
            try:
                checksum_request = Request(checksum_url, headers={"User-Agent": "yikou-light-food"})
                with (opener or urlopen)(checksum_request, timeout=timeout) as response:
                    expected_hash = response.read().decode("ascii").strip().split()[0].lower()
            except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, ValueError, IndexError) as exc:
                expected_hash = embedded_hash
                if not expected_hash:
                    raise UpdateError(f"Unable to verify update checksum: {exc}") from exc
        else:
            expected_hash = embedded_hash
            if not expected_hash:
                raise UpdateError("Release does not contain a trusted SHA-256 checksum")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("invalid SHA-256")
        actual_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise UpdateError("Downloaded update failed SHA-256 verification")
    except UpdateError:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, ValueError, IndexError) as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise UpdateError(f"Unable to verify update checksum: {exc}") from exc

    # Reject path-traversal entries before extracting, then use ditto so that
    # symlinks, permissions and extended attributes inside the .app survive.
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise UpdateError("更新包包含非法路径条目")
    except (zipfile.BadZipFile, OSError, UpdateError) as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise UpdateError(f"Unable to read update archive: {exc}") from exc
    if stage_callback:
        stage_callback("解压更新")
    extract_dir = workdir / "extracted"
    extract_dir.mkdir()
    try:
        subprocess.run(
            ["/usr/bin/ditto", "-x", "-k", str(archive), str(extract_dir)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise UpdateError(f"Unable to extract update: {exc}") from exc

    new_app = next((p for p in extract_dir.iterdir() if p.suffix.lower() == ".app" and p.is_dir()), None)
    if new_app is None or not (new_app / "Contents" / "MacOS").is_dir():
        shutil.rmtree(workdir, ignore_errors=True)
        raise UpdateError("更新包中未找到有效的 .app 应用包")

    if stage_callback:
        stage_callback("准备重启")
    pid = os.getpid()
    script = (
        f"while kill -0 {pid} 2>/dev/null; do sleep 0.3; done; "
        f"rm -rf {shlex.quote(str(app_bundle))}; "
        f"ditto {shlex.quote(str(new_app))} {shlex.quote(str(app_bundle))}; "
        f"open {shlex.quote(str(app_bundle))}; "
        f"rm -rf {shlex.quote(str(workdir))}"
    )
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise UpdateError(f"Unable to start update installer: {exc}") from exc
    return app_bundle


def _download_and_install_linux(
    release: ReleaseInfo,
    *,
    timeout: float = 60.0,
    opener: Callable[..., Any] | None = None,
    progress_callback: Callable[[int, int | None], None] | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> Path:
    """Download the Linux tar.gz, verify it, and swap the binary after exit.

    The Linux package is a single PyInstaller onefile executable inside a
    tar.gz.  Linux does not lock the running binary (rename() over a running
    executable is allowed), but the safest approach mirrors the macOS helper:
    a detached shell waits for this process to exit, atomically renames the
    staged file over the old executable and execs the new version.
    """
    if not getattr(sys, "frozen", False):
        raise UpdateError("源码运行模式不支持自动安装，请前往 GitHub Release 页面下载")
    asset = release.linux_asset
    asset_name = str(asset.get("name") if asset else "")
    url = _asset_url(asset)
    if not safe_asset_name(asset_name) or not asset_name.lower().endswith(".tar.gz") or not _trusted_url(url):
        raise UpdateError("Release does not contain a trusted Linux archive")
    target = Path(sys.executable).resolve()
    install_dir = target.parent
    if not install_dir.exists() or not os.access(install_dir, os.W_OK):
        raise UpdateError("安装目录不可写，请将程序移动到可写目录后重试")
    # 优先走差分更新：本地二进制的 SHA-256 命中补丁基线时只需下载小补丁。
    patch = _find_applicable_patch(release, target)
    if patch is not None:
        return _download_and_apply_patch_linux(
            release, patch, target,
            timeout=timeout, opener=opener,
            progress_callback=progress_callback, stage_callback=stage_callback,
        )
    expected_size: int | None = None
    try:
        expected_size = int(asset.get("size")) if asset and asset.get("size") is not None else None
    except (TypeError, ValueError):
        expected_size = None
    if expected_size and expected_size > MAX_UPDATE_SIZE:
        raise UpdateError("更新资源大小异常")
    try:
        # 预留下载归档、解压出的可执行文件和余量；归档是 gzip 压缩的，
        # 解压后的文件更大，按归档大小的 4 倍预留是保守估计。
        required_space = (expected_size or 100_000_000) * 4 + 1_048_576
        if shutil.disk_usage(install_dir).free < required_space:
            raise UpdateError("磁盘剩余空间不足，无法安装更新")
    except OSError as exc:
        raise UpdateError(f"无法检查磁盘空间：{exc}") from exc

    workdir = Path(tempfile.mkdtemp(prefix="yikou-light-food-update-"))
    staging = install_dir / f".{target.stem}.update-{os.getpid()}"
    archive = workdir / asset_name
    # 上次更新中断可能留下同名 staging；清掉避免解压冲突。
    if staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
    elif staging.exists():
        staging.unlink(missing_ok=True)
    try:
        if stage_callback:
            stage_callback("下载更新")
        last_error: Exception | None = None
        for candidate in _github_url_candidates(url):
            archive.unlink(missing_ok=True)
            try:
                _stream_download(
                    candidate,
                    archive,
                    timeout=timeout,
                    opener=opener or urlopen,
                    expected_size=expected_size,
                    progress_callback=progress_callback,
                )
                last_error = None
                break
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, UpdateError) as exc:
                last_error = exc
        if last_error is not None:
            raise UpdateError(f"Unable to download update: {last_error}") from last_error

        checksum_asset = release.linux_checksum_asset
        checksum_url = str((checksum_asset or {}).get("sha256_url") or _asset_url(checksum_asset))
        # latest.json 内嵌的官方哈希：GitHub 直连不可达（镜像存在的场景）时，
        # 校验文件同样拉不到，此时回退到清单内嵌的同一 SHA-256。
        embedded_hash = _embedded_asset_sha256(release.linux_asset)
        try:
            if stage_callback:
                stage_callback("校验安装包")
            if _trusted_url(checksum_url):
                try:
                    checksum_request = Request(checksum_url, headers={"User-Agent": "yikou-light-food"})
                    with (opener or urlopen)(checksum_request, timeout=timeout) as response:
                        expected_hash = response.read().decode("ascii").strip().split()[0].lower()
                except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, ValueError, IndexError) as exc:
                    expected_hash = embedded_hash
                    if not expected_hash:
                        raise UpdateError(f"Unable to verify update checksum: {exc}") from exc
            else:
                expected_hash = embedded_hash
                if not expected_hash:
                    raise UpdateError("Release does not contain a trusted SHA-256 checksum")
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                raise ValueError("invalid SHA-256")
            actual_hash = _sha256_file(archive)
            if actual_hash != expected_hash:
                raise UpdateError("Downloaded update failed SHA-256 verification")
        except UpdateError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, ValueError, IndexError) as exc:
            raise UpdateError(f"Unable to verify update checksum: {exc}") from exc

        if stage_callback:
            stage_callback("解压更新")
        staged_binary = _extract_linux_binary(archive, staging)

        return _schedule_linux_replacement(staged_binary, target, workdir, staging, stage_callback)
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _extract_linux_binary(archive: Path, destination: Path) -> Path:
    """解压 release tar.gz 到 destination，返回包内的应用可执行文件。

    解压前逐个校验成员：拒绝绝对路径、``..`` 上跳与链接/设备等特殊条目；
    再交给 ``tarfile`` 的 ``data`` 过滤器兜底（Python 3.10.12+/3.12+ 可用，
    更旧的解释器上成员已校验过，直接解压也是安全的）。
    """
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            name = str(member.name or "")
            if not name or name.startswith("/") or ".." in Path(name).parts:
                raise UpdateError("更新包包含非法路径条目")
            if member.issym() or member.islnk() or not (member.isreg() or member.isdir()):
                raise UpdateError("更新包含有意外条目类型")
        try:
            tar.extractall(destination, members=members, filter="data")
        except TypeError:  # Python < 3.12 无过滤参数
            tar.extractall(destination, members=members)
    binaries = [path for path in sorted(destination.rglob("yikou-light-food")) if path.is_file()]
    if not binaries:
        raise UpdateError("更新包中未找到应用可执行文件")
    if len(binaries) > 1:
        raise UpdateError("更新包结构异常")
    binary = binaries[0]
    # data 过滤器会保留执行位，但显式 chmod 不依赖过滤器行为。
    binary.chmod(0o755)
    return binary


def _schedule_linux_replacement(
    staged_binary: Path,
    target: Path,
    workdir: Path,
    staging_dir: Path,
    stage_callback: Callable[[str], None] | None = None,
    *,
    pid: int | None = None,
    launcher: Callable[..., Any] | None = None,
) -> Path:
    """Spawn a detached shell that swaps in the new binary after exit.

    The shell waits for the current process (``pid``) to disappear, renames
    the staged file over the installed executable (atomic, same filesystem)
    and execs it.  Staging inside the install directory keeps the rename on
    one filesystem; the temp workdir is removed either way.
    """
    if stage_callback:
        stage_callback("准备重启")
    popen = launcher or subprocess.Popen
    pid = os.getpid() if pid is None else pid
    installed = shlex.quote(str(target))
    temp = f"{shlex.quote(str(workdir))} {shlex.quote(str(staging_dir))}"
    script = (
        f"while kill -0 {pid} 2>/dev/null; do sleep 0.3; done; "
        f"if mv -f {shlex.quote(str(staged_binary))} {installed}; then "
        f"rm -rf {temp} & exec {installed}; fi; rm -rf {temp}"
    )
    try:
        popen(
            ["/bin/sh", "-c", script],
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
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
