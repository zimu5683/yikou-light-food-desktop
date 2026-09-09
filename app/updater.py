"""GitHub Releases based update checker.

The checker is intentionally small and dependency-free so it also works from
the PyInstaller executable. Network failures are reported to the caller and
never prevent the main application from starting.
"""
from __future__ import annotations

import base64
import json
import logging
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
from .update_health import (begin_update_health_check, clear_update_health,
                            wait_for_health)

logger = logging.getLogger(__name__)

REPOSITORY = "zimu5683/yikou-light-food-desktop"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
LATEST_MANIFEST_URL = f"https://github.com/{REPOSITORY}/releases/latest/download/latest.json"
LATEST_MANIFEST_SIGNATURE_URL = f"{LATEST_MANIFEST_URL}.sig"
# 内置 Ed25519 公钥：仅由它验证 latest.json 的真实性，SHA-256 只用于完整性。
# 私钥保存在发布方机器/CI Secret（UPDATE_SIGNING_KEY）中，绝不进入仓库。
UPDATE_MANIFEST_PUBLIC_KEY = "dnIqg6Tj0ytkB4mk/2I1fbLrpf55TRAH2EyhR73LTHo="
UPDATE_MANIFEST_KEY_ID = "5b9ba0388caffa07"


def _load_update_trust() -> tuple[str, str, bool]:
    """Load public code-signing trust anchors from the packaged config.

    The values are written into ``app/update_trust.json`` during release so the
    final installed app does not depend on end-user environment variables.
    Environment variables still override for development/testing.
    ``allow_unsigned_update`` is an explicit self-use switch: when no
    Authenticode/Team ID anchor is available, the updater may skip only the OS
    publisher check.  Signed manifest and SHA-256 checks are always enforced.
    """
    data: dict[str, Any] = {}
    try:
        data = json.loads((Path(__file__).with_name("update_trust.json")).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        data = {}
    windows = (
        os.environ.get("YIKOU_WINDOWS_AUTHENTICODE_PUBLISHER")
        or data.get("windows_authenticode_publisher")
        or ""
    )
    macos = (
        os.environ.get("YIKOU_MACOS_TEAM_ID")
        or data.get("macos_team_id")
        or ""
    )
    raw_allow = os.environ.get("YIKOU_ALLOW_UNSIGNED_UPDATE")
    if raw_allow is None:
        allow_unsigned = bool(data.get("allow_unsigned_update", False))
    else:
        allow_unsigned = str(raw_allow).strip().lower() not in {"0", "false", "no", "off", ""}
    return str(windows).strip(), str(macos).strip(), allow_unsigned


# 发布者签名主体/Team ID 打包时写入；未配置时默认 fail-closed。
# 个人自用构建可显式设置 allow_unsigned_update=true：仍强制校验签名清单与 SHA-256。
WINDOWS_AUTHENTICODE_PUBLISHER, MACOS_TEAM_ID, ALLOW_UNSIGNED_UPDATE = _load_update_trust()
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
    minimum_supported_version: str = ""
    manifest_sha256: str = ""
    manifest_signature: str = ""
    manifest_key_id: str = ""
    require_platform_fields: bool = False

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


def _is_windows_platform() -> bool:
    return os.name == "nt"


def _is_macos_platform() -> bool:
    return sys.platform == "darwin"


def _is_linux_platform() -> bool:
    return sys.platform.startswith("linux")


def _current_architecture(platform_name: str) -> str:
    if platform_name == "windows":
        return "arm64" if os.environ.get("PROCESSOR_ARCHITECTURE", "").lower() == "arm64" else "x64"
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    return "arm64" if machine in {"arm64", "aarch64"} else "x64"


def _asset_matches_platform(asset: dict[str, Any] | None, platform_name: str,
                            architecture: str, *, require_declared: bool = False) -> bool:
    """校验清单条目声明的平台/架构。

    ``require_declared=True`` 时（新清单 ``requires_platform_metadata``）
    未声明 platform/architecture 的条目直接拒绝，避免“没字段就放行”。
    """
    if not isinstance(asset, dict):
        return False
    declared_platform = str(asset.get("platform") or "").strip().lower()
    declared_arch = str(asset.get("architecture") or asset.get("arch") or "").strip().lower()
    if require_declared and (not declared_platform or not declared_arch):
        return False
    if declared_platform and declared_platform not in {platform_name, "any"}:
        return False
    aliases = {architecture, "any", "universal"}
    if architecture == "x64":
        aliases.update({"amd64", "x86_64"})
    if declared_arch and declared_arch not in aliases:
        return False
    return True


def _resolve_asset_hash(asset: dict[str, Any] | None, checksum_url: str, *,
                        opener: Callable[..., Any], timeout: float,
                        stage_callback: Callable[[str], None] | None = None) -> str:
    """Return the expected SHA-256 for an asset.

    The signed manifest is the trust root, so an embedded hash always wins over
    a separately fetched ``.sha256`` file.  The latter is only a compatibility
    fallback for old releases that do not embed hashes.
    """
    if stage_callback:
        stage_callback("校验安装包")
    embedded = _embedded_asset_sha256(asset)
    if embedded:
        return embedded
    if not _trusted_url(checksum_url):
        raise UpdateError("Release does not contain a trusted SHA-256 checksum")
    try:
        checksum_request = Request(checksum_url, headers={"User-Agent": "yikou-light-food"})
        with opener(checksum_request, timeout=timeout) as response:
            return response.read().decode("ascii").strip().split()[0].lower()
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, ValueError, IndexError) as exc:
        raise UpdateError(f"Unable to verify update checksum: {exc}") from exc


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
        if not _asset_matches_platform(asset, platform, architecture):
            continue
        if platform == "windows" and lower.endswith(".exe") and "macos" not in lower and "linux" not in lower:
            result.append(asset)
        elif platform == "macos" and lower.endswith(".zip") and "macos" in lower and (architecture in lower or "arm64" not in lower and "x64" not in lower):
            result.append(asset)
        elif platform == "linux" and lower.endswith((".appimage", ".tar.gz", ".deb")) and "linux" in lower:
            result.append(asset)
    return tuple(result)


_SEMVER_RE = re.compile(
    r"^v?\d+\.\d+\.\d+"
    r"(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?"
    r"(?:\+[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?$"
)


def normalize_version(value: str) -> str:
    """Return a comparable version string (``v1.2.3`` -> ``1.2.3``)."""
    text = str(value or "0").strip()
    return text[1:] if text.lower().startswith("v") else text


def is_valid_semver(value: str) -> bool:
    """严格 SemVer（允许可选的 v 前缀），拒绝 ``1.2``、``latest`` 等格式。"""
    return bool(_SEMVER_RE.fullmatch(str(value or "").strip()))


def _version_parts(value: str) -> tuple[tuple[int, ...], tuple[str, ...]]:
    value = normalize_version(value)
    # Ignore build metadata; compare prerelease identifiers according to the
    # SemVer rule where a release is newer than its prerelease.
    value = value.split("+", 1)[0]
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


def _decode_manifest(payload: Any, *, source_url: str = LATEST_MANIFEST_URL,
                     manifest_sha256: str = "", manifest_signature: str = "",
                     manifest_key_id: str = "") -> ReleaseInfo:
    if not isinstance(payload, dict):
        raise UpdateError("latest.json 格式无效")
    schema = payload.get("schema_version", 1)
    if str(schema) not in {"1", "1.0"}:
        raise UpdateError("latest.json schema_version 不受支持")
    version = str(payload.get("version") or payload.get("tag_name") or "").strip()
    if not version:
        raise UpdateError("latest.json 缺少 version")
    if not is_valid_semver(version):
        raise UpdateError(f"latest.json 版本号不是严格 SemVer：{version}")
    minimum = str(payload.get("minimum_supported_version") or "").strip()
    if minimum and not is_valid_semver(minimum):
        raise UpdateError(f"latest.json minimum_supported_version 格式异常：{minimum}")
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
    require_platform = bool(payload.get("requires_platform_metadata"))
    return ReleaseInfo(
        tag_name=version if version.lower().startswith("v") else f"v{version}",
        name=str(payload.get("name") or version),
        body=str(payload.get("body") or payload.get("release_summary") or payload.get("notes") or "").strip(),
        html_url=str(payload.get("url") or payload.get("html_url") or ""),
        assets=tuple(assets),
        patches=patches,
        manifest_source="manifest",
        manifest_url=source_url,
        minimum_supported_version=minimum,
        manifest_sha256=manifest_sha256,
        manifest_signature=manifest_signature,
        manifest_key_id=manifest_key_id,
        require_platform_fields=require_platform,
    )


def _parse_manifest_signature(signature_bytes: bytes) -> tuple[str, str]:
    """Return ``(key_id, signature_b64)`` from JSON/base64/raw .sig content."""
    try:
        text = signature_bytes.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        if len(signature_bytes) == 64:
            return "", base64.b64encode(signature_bytes).decode("ascii")
        raise UpdateError("latest.json.sig 不是有效的文本签名")
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        key_id = str(parsed.get("key_id") or parsed.get("keyId") or "").strip()
        signature = str(parsed.get("signature") or parsed.get("sig") or "").strip()
        if not signature:
            raise UpdateError("latest.json.sig 缺少 signature 字段")
        return key_id, signature
    parts = text.split()
    if len(parts) >= 2:
        # 兼容 ``<key_id> <base64>`` 与 ``<base64> <key_id>`` 两种写法。
        if re.fullmatch(r"[0-9a-fA-F]{8,64}", parts[0]):
            return parts[0], parts[1]
        if re.fullmatch(r"[0-9a-fA-F]{8,64}", parts[-1]):
            return parts[-1], parts[0]
    return "", text


def verify_manifest_signature(manifest_bytes: bytes, signature_bytes: bytes) -> tuple[str, str]:
    """用内置 Ed25519 公钥验证清单；返回 ``(key_id, manifest_sha256)``。"""
    if not UPDATE_MANIFEST_PUBLIC_KEY:
        raise UpdateError("客户端未内置更新签名公钥，拒绝未签名清单")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - requirements.txt 已固定依赖
        raise UpdateError("缺少 cryptography，无法验证更新清单签名") from exc

    key_id, signature_b64 = _parse_manifest_signature(signature_bytes)
    if key_id and key_id.lower() != UPDATE_MANIFEST_KEY_ID.lower():
        raise UpdateError(f"更新清单签名 key_id 不匹配：{key_id}")
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except Exception as exc:  # noqa: BLE001 - binascii.Error 等统一转成 UpdateError
        raise UpdateError("latest.json.sig 不是有效的 Base64 签名") from exc
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(UPDATE_MANIFEST_PUBLIC_KEY))
    except Exception as exc:  # noqa: BLE001 - 公钥损坏必须 fail-closed
        raise UpdateError("内置更新公钥格式无效") from exc
    try:
        public_key.verify(signature, manifest_bytes)
    except InvalidSignature as exc:
        raise UpdateError("更新清单签名验证失败，拒绝使用该 latest.json") from exc
    return key_id or UPDATE_MANIFEST_KEY_ID, hashlib.sha256(manifest_bytes).hexdigest()


def _fetch_bytes(url: str, *, timeout: float, opener: Callable[..., Any]) -> bytes:
    """按 [直连, 镜像1, 镜像2...] 取字节流；镜像只是传输层。"""
    last_error: Exception | None = None
    for candidate in _github_url_candidates(url):
        for attempt in range(2):
            request = Request(candidate, headers={"Accept": "application/octet-stream",
                                                  "User-Agent": "yikou-light-food"})
            try:
                with opener(request, timeout=timeout) as response:
                    return response.read()
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.4)
    if last_error is None:
        raise UpdateError(f"无法获取更新元数据：{url}")
    if len(_github_url_candidates(url)) == 1:
        raise last_error
    raise UpdateError(f"Unable to fetch update metadata: {last_error}") from last_error


def _fetch_json(url: str, *, timeout: float, opener: Callable[..., Any]) -> Any:
    """按 [直连, 镜像1, 镜像2...] 依次尝试；每个源内部重试 1 次，抵御网络瞬断。"""
    return json.loads(_fetch_bytes(url, timeout=timeout, opener=opener).decode("utf-8"))


def _fetch_verified_manifest(*, timeout: float, opener: Callable[..., Any]) -> ReleaseInfo:
    manifest_bytes = _fetch_bytes(LATEST_MANIFEST_URL, timeout=timeout, opener=opener)
    signature_bytes = _fetch_bytes(LATEST_MANIFEST_SIGNATURE_URL, timeout=timeout, opener=opener)
    key_id, manifest_sha = verify_manifest_signature(manifest_bytes, signature_bytes)
    try:
        payload = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise UpdateError("latest.json 不是有效 UTF-8 JSON") from exc
    return _decode_manifest(
        payload,
        source_url=LATEST_MANIFEST_URL,
        manifest_sha256=manifest_sha,
        manifest_signature=base64.b64encode(signature_bytes).decode("ascii"),
        manifest_key_id=key_id,
    )


def check_for_update(
    current_version: str = __version__,
    *,
    timeout: float = 5.0,
    opener: Callable[..., Any] | None = None,
) -> ReleaseInfo | None:
    """获取并验证签名清单；只返回严格高于当前版本的更新。"""
    open_func = opener or urlopen
    if not is_valid_semver(current_version):
        raise UpdateError(f"当前版本号不是严格 SemVer，拒绝更新：{current_version}")
    try:
        release = _fetch_verified_manifest(timeout=timeout, opener=open_func)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, UnicodeError, UpdateError) as exc:
        raise UpdateError(f"Unable to check for updates: 更新清单验证失败：{exc}") from exc

    if release.minimum_supported_version and compare_versions(
            current_version, release.minimum_supported_version) < 0:
        raise UpdateError(
            f"当前版本 {current_version} 低于最低支持版本 {release.minimum_supported_version}，"
            "请前往 GitHub Release 手动下载完整安装包")
    comparison = compare_versions(release.version, current_version)
    if comparison < 0:
        raise UpdateError(f"更新清单版本 {release.version} 低于当前版本 {current_version}，拒绝降级")
    if comparison == 0:
        # 同版本永不自动覆盖；即使发布方重新打包，也不做静默替换。
        return None
    return release


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
    if _is_macos_platform():
        return _download_and_install_macos(
            release,
            timeout=timeout,
            opener=opener,
            progress_callback=progress_callback,
            stage_callback=stage_callback,
        )
    if _is_linux_platform():
        return _download_and_install_linux(
            release,
            timeout=timeout,
            opener=opener,
            progress_callback=progress_callback,
            stage_callback=stage_callback,
        )
    if not _is_windows_platform():
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
    if not _asset_matches_platform(
            asset, "windows", _current_architecture("windows"),
            require_declared=release.require_platform_fields):
        raise UpdateError("更新包平台或架构与当前 Windows 版本不匹配")
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
    # 校验文件同样拉不到，此时回退到已通过 Ed25519 签名验证的清单内嵌哈希。
    # 镜像只能提供字节流，不能改变受签名保护的 SHA-256。
    try:
        expected_hash = _resolve_asset_hash(
            release.executable_asset, checksum_url, opener=opener or urlopen,
            timeout=timeout, stage_callback=stage_callback)
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

    _verify_windows_authenticode(temporary)
    # A running executable cannot replace itself on Windows.  Launch a copy
    # of the freshly downloaded version from the user's temporary directory;
    # that copy waits for this process to exit, atomically replaces the old
    # executable, and starts the installed copy.  This avoids PowerShell and
    # works with Chinese paths.
    return _schedule_windows_replacement(temporary, target, stage_callback)


def _verify_windows_authenticode(path: Path) -> None:
    """强制校验 Windows Authenticode 发布者；未配置发布者时 fail-closed。

    源码/测试模式不会真正替换用户安装，允许跳过；打包版必须通过校验。
    """
    if not getattr(sys, "frozen", False) and os.environ.get("YIKOU_REQUIRE_CODE_SIGNING") != "1":
        logger.warning("非打包模式跳过 Windows Authenticode 校验")
        return
    publisher = str(WINDOWS_AUTHENTICODE_PUBLISHER or "").strip()
    if not publisher:
        if ALLOW_UNSIGNED_UPDATE:
            logger.warning("个人自用配置允许未签名更新：跳过 Windows Authenticode 发布者校验")
            return
        raise UpdateError(
            "未配置 Windows Authenticode 发布者（YIKOU_WINDOWS_AUTHENTICODE_PUBLISHER），"
            "拒绝安装未验证发布者签名的更新")
    if not _is_windows_platform():
        raise UpdateError("当前平台无法执行 Windows Authenticode 校验")
    escaped = str(path).replace("'", "''")
    script = (
        f"$sig = Get-AuthenticodeSignature -LiteralPath '{escaped}'; "
        "if ($sig.Status -ne 'Valid') { Write-Output \"STATUS:$($sig.Status)\"; exit 1 }; "
        "Write-Output $sig.SignerCertificate.Subject"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"Windows Authenticode 校验无法执行：{exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stdout or completed.stderr or "").strip()
        raise UpdateError(f"Windows Authenticode 签名无效：{detail or 'Unknown'}")
    subject = (completed.stdout or "").strip()
    if publisher.casefold() not in subject.casefold():
        raise UpdateError(f"Windows 发布者不匹配：期望 {publisher!r}，实际 {subject!r}")


def _verify_macos_bundle(app_bundle: Path) -> None:
    """校验 codesign、Team ID 与 notarization/Gatekeeper 评估。

    源码/测试模式不会真正替换用户安装，允许跳过；打包版必须通过校验。
    """
    if not getattr(sys, "frozen", False) and os.environ.get("YIKOU_REQUIRE_CODE_SIGNING") != "1":
        logger.warning("非打包模式跳过 macOS codesign 校验")
        return
    team_id = str(MACOS_TEAM_ID or "").strip()
    if not team_id:
        if ALLOW_UNSIGNED_UPDATE:
            logger.warning("个人自用配置允许未签名更新：跳过 macOS codesign/Team ID 校验")
            return
        raise UpdateError(
            "未配置 macOS Team ID（YIKOU_MACOS_TEAM_ID），拒绝安装未验证签名的更新")
    if not _is_macos_platform():
        raise UpdateError("当前平台无法执行 macOS codesign 校验")
    commands = [
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app_bundle)],
        ["/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=4", str(app_bundle)],
    ]
    for command in commands:
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UpdateError(f"macOS 签名校验无法执行：{exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise UpdateError(f"macOS 签名/公证校验失败：{detail or command[0]}")
    try:
        details = subprocess.run(
            ["/usr/bin/codesign", "-dv", "--verbose=4", str(app_bundle)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"无法读取 macOS Team ID：{exc}") from exc
    output = f"{details.stdout}\n{details.stderr}"
    match = re.search(r"TeamIdentifier=([^\s]+)", output)
    if not match or match.group(1) != team_id:
        actual = match.group(1) if match else "未找到"
        raise UpdateError(f"macOS Team ID 不匹配：期望 {team_id}，实际 {actual}")


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

    _verify_windows_authenticode(temporary)
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
    if not _asset_matches_platform(
            asset, "macos", _current_architecture("macos"),
            require_declared=release.require_platform_fields):
        raise UpdateError("更新包平台或架构与当前 macOS 版本不匹配")
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
    try:
        expected_hash = _resolve_asset_hash(
            release.macos_asset, checksum_url, opener=opener or urlopen,
            timeout=timeout, stage_callback=stage_callback)
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
    _verify_macos_bundle(new_app)

    if stage_callback:
        stage_callback("准备重启")
    pid = os.getpid()
    new_binary = new_app / "Contents" / "MacOS" / "yikou-light-food"
    app_path = shlex.quote(str(app_bundle))
    backup_path = shlex.quote(str(app_bundle) + ".bak")
    health_enabled = (
        getattr(sys, "frozen", False)
        and os.environ.get("YIKOU_SKIP_UPDATE_HEALTH_CHECK") != "1"
    )
    if health_enabled:
        health_token, health_marker = begin_update_health_check(tempfile.gettempdir())
        marker = shlex.quote(health_marker)
        token = shlex.quote(health_token)
        health_env = f"YIKOU_UPDATE_HEALTH_FILE={marker} YIKOU_UPDATE_HEALTH_TOKEN={token} "
        script = (
            f"while kill -0 {pid} 2>/dev/null; do sleep 0.3; done; "
            f"if {shlex.quote(str(new_binary))} --self-check >/dev/null 2>&1; then "
            f"rm -rf {backup_path}; "
            f"if [ -e {app_path} ]; then mv {app_path} {backup_path}; fi; "
            f"if ditto {shlex.quote(str(new_app))} {app_path}; then "
            f"rm -f {marker}; "
            f"{health_env}{shlex.quote(str(new_binary))} >/dev/null 2>&1 & new_pid=$!; "
            f"ok=0; i=0; while [ $i -lt 60 ]; do "
            f"if grep -F {token} {marker} >/dev/null 2>&1; then ok=1; break; fi; "
            f"if ! kill -0 $new_pid 2>/dev/null; then break; fi; "
            f"sleep 1; i=$((i+1)); done; rm -f {marker}; "
            f"if [ $ok -eq 1 ]; then rm -rf {backup_path}; rm -rf {shlex.quote(str(workdir))}; exit 0; fi; "
            f"kill $new_pid 2>/dev/null || true; rm -rf {app_path}; "
            f"if [ -e {backup_path} ]; then mv {backup_path} {app_path}; open {app_path}; fi; "
            f"rm -rf {shlex.quote(str(workdir))}; exit 1; "
            f"else rm -rf {app_path}; "
            f"if [ -e {backup_path} ]; then mv {backup_path} {app_path}; open {app_path}; fi; fi; "
            f"fi; rm -rf {shlex.quote(str(workdir))}"
        )
    else:
        script = (
            f"while kill -0 {pid} 2>/dev/null; do sleep 0.3; done; "
            f"if {shlex.quote(str(new_binary))} --self-check >/dev/null 2>&1; then "
            f"rm -rf {backup_path}; "
            f"if [ -e {app_path} ]; then mv {app_path} {backup_path}; fi; "
            f"if ditto {shlex.quote(str(new_app))} {app_path} && open {app_path}; then "
            f"rm -rf {backup_path}; "
            f"else rm -rf {app_path}; "
            f"if [ -e {backup_path} ]; then mv {backup_path} {app_path}; open {app_path}; fi; fi; "
            f"fi; rm -rf {shlex.quote(str(workdir))}"
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
    if not _asset_matches_platform(
            asset, "linux", _current_architecture("linux"),
            require_declared=release.require_platform_fields):
        raise UpdateError("更新包平台或架构与当前 Linux 版本不匹配")
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
        # latest.json 内嵌的官方哈希来自已签名清单，优先级高于独立 .sha256。
        try:
            expected_hash = _resolve_asset_hash(
                release.linux_asset, checksum_url, opener=opener or urlopen,
                timeout=timeout, stage_callback=stage_callback)
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
            if member.isreg():
                # 发布包只允许单个应用二进制；任何额外文件（尤其是可执行文件）
                # 都可能是夹带物，直接拒绝而不是解压后再筛选。
                if name != "yikou-light-food":
                    raise UpdateError(f"更新包包含预期外文件：{name}")
                if member.mode & 0o7000:
                    raise UpdateError("更新包中的应用文件带有 setuid/setgid 位")
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
    health_timeout: float = 60.0,
) -> Path:
    """Spawn a detached shell that swaps in the new binary after exit.

    The shell waits for the current process to exit, self-checks the staged
    binary, replaces it, launches it and waits for the GUI startup marker.  If
    the marker never appears, the previous binary is restored and relaunched.
    """
    if stage_callback:
        stage_callback("准备重启")
    popen = launcher or subprocess.Popen
    pid = os.getpid() if pid is None else pid
    installed = shlex.quote(str(target))
    backup = shlex.quote(str(target) + ".bak")
    temp = f"{shlex.quote(str(workdir))} {shlex.quote(str(staging_dir))}"
    staged = shlex.quote(str(staged_binary))

    if getattr(sys, "frozen", False) and os.environ.get("YIKOU_SKIP_UPDATE_HEALTH_CHECK") != "1":
        health_token, health_marker = begin_update_health_check(tempfile.gettempdir())
        marker = shlex.quote(health_marker)
        token = shlex.quote(health_token)
        health_env = f"YIKOU_UPDATE_HEALTH_FILE={marker} YIKOU_UPDATE_HEALTH_TOKEN={token} "
        checks = max(1, int(health_timeout))
        script = (
            f"while kill -0 {pid} 2>/dev/null; do sleep 0.3; done; "
            f"if {staged} --self-check >/dev/null 2>&1; then "
            f"cp -f {installed} {backup} 2>/dev/null || true; "
            f"if mv -f {staged} {installed}; then "
            f"rm -f {marker}; "
            f"{health_env}{installed} >/dev/null 2>&1 & new_pid=$!; "
            f"ok=0; i=0; "
            f"while [ $i -lt {checks} ]; do "
            f"if grep -F {token} {marker} >/dev/null 2>&1; then ok=1; break; fi; "
            f"if ! kill -0 $new_pid 2>/dev/null; then break; fi; "
            f"sleep 1; i=$((i+1)); done; "
            f"rm -f {marker}; "
            f"if [ $ok -eq 1 ]; then rm -rf {temp}; exit 0; fi; "
            f"kill $new_pid 2>/dev/null || true; "
            f"cp -f {backup} {installed} 2>/dev/null || true; "
            f"{installed} >/dev/null 2>&1 & rm -rf {temp}; exit 1; "
            f"fi; fi; "
            f"cp -f {backup} {installed} 2>/dev/null || true; rm -rf {temp}"
        )
    else:
        script = (
            f"while kill -0 {pid} 2>/dev/null; do sleep 0.3; done; "
            f"if {staged} --self-check >/dev/null 2>&1; then "
            f"cp -f {installed} {backup} 2>/dev/null || true; "
            f"if mv -f {staged} {installed}; then "
            f"rm -rf {temp} & exec {installed}; fi; fi; "
            f"cp -f {backup} {installed} 2>/dev/null || true; rm -rf {temp}"
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


def _run_startup_self_check(executable: Path) -> None:
    """在替换旧版本前运行新产物的 --self-check；失败则中止更新。"""
    if os.environ.get("YIKOU_SKIP_UPDATE_SELF_CHECK") == "1":
        return
    if not getattr(sys, "frozen", False):
        return
    try:
        if executable.stat().st_size < 1_000_000:
            return
    except OSError:
        return
    try:
        completed = subprocess.run(
            [str(executable), "--self-check"],
            capture_output=True, text=True, timeout=60, check=False,
            cwd=str(executable.parent),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"新版本启动自检无法执行：{exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stdout or completed.stderr or "").strip()
        raise UpdateError(f"新版本启动自检失败，保留当前版本：{detail or completed.returncode}")


def apply_pending_update(
    source: str | os.PathLike[str],
    target: str | os.PathLike[str],
    *,
    timeout: float = 120.0,
    retry_interval: float = 0.5,
    health_timeout: float = 60.0,
    launcher: Callable[..., Any] = subprocess.Popen,
) -> Path:
    """Replace ``target`` with a verified downloaded exe and restart it.

    This runs inside the temporary updater copy, not inside the installed
    executable.  Retrying ``os.replace`` is both a lock check and an atomic
    replacement once the original GUI process has fully exited.  In frozen
    mode the new process must write a startup health marker, otherwise the
    old executable is restored and relaunched.
    """
    source_path = Path(source).resolve()
    target_path = Path(target).resolve()
    if not source_path.is_file() or target_path.suffix.lower() != ".exe":
        raise UpdateError("Pending update files are invalid")
    _run_startup_self_check(source_path)
    backup_path = target_path.with_name(target_path.name + ".bak")
    try:
        if target_path.is_file():
            shutil.copy2(target_path, backup_path)
    except OSError as exc:
        raise UpdateError(f"无法备份当前版本，拒绝替换：{exc}") from exc
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

    health_enabled = (
        getattr(sys, "frozen", False)
        and os.environ.get("YIKOU_SKIP_UPDATE_HEALTH_CHECK") != "1"
    )
    health_token = ""
    health_marker = ""
    launch_env: dict[str, str] | None = None
    if health_enabled:
        health_token, health_marker = begin_update_health_check(tempfile.gettempdir())
        launch_env = {
            **os.environ,
            "YIKOU_UPDATE_HEALTH_FILE": health_marker,
            "YIKOU_UPDATE_HEALTH_TOKEN": health_token,
        }
    launch_kwargs: dict[str, Any] = {
        "cwd": str(target_path.parent),
        "close_fds": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if launch_env is not None:
        launch_kwargs["env"] = launch_env
    try:
        launcher([str(target_path)], **launch_kwargs)
    except OSError as exc:
        # 新版本连启动进程都拉不起来时，立即回滚到上一版，避免应用无法启动。
        clear_update_health(health_marker)
        try:
            shutil.copy2(backup_path, target_path)
        except OSError:
            pass
        raise UpdateError(f"Update installed but the application could not restart: {exc}") from exc

    if health_enabled:
        healthy = wait_for_health(health_marker, health_token, timeout=health_timeout)
        clear_update_health(health_marker)
        if not healthy:
            try:
                shutil.copy2(backup_path, target_path)
            except OSError as exc:
                raise UpdateError(f"新版本启动健康检查失败，且无法恢复旧版本：{exc}") from exc
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
                raise UpdateError(f"新版本启动健康检查失败，旧版本也无法重新启动：{exc}") from exc
            raise UpdateError("新版本启动健康检查失败，已回滚到上一版本")

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
