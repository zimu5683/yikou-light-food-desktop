"""Generate a bsdiff patch against the previous release's binary.

Runs in the Windows CI job after ``PyInstaller`` has produced
``dist/yikou-light-food.exe`` and before the Release is published.  It:

1. finds the previous (non-prerelease, non-draft) release via ``gh api``;
2. downloads that release's ``yikou-light-food.exe``;
3. generates ``dist/yikou-light-food-{previous}-{current}.patch`` with bsdiff4;
4. writes ``dist/patch-meta.json`` describing the patch so
   ``generate_latest_manifest.py`` can expose it in ``latest.json``.

With the ``linux`` argument the same flow runs in the Linux CI job: it
downloads the previous release's ``yikou-light-food-linux-x64.tar.gz``,
extracts the raw onefile binary (that file is what users have locally, so it
is the patch baseline) and diffs it against ``dist/yikou-light-food``.  The
metadata lands in ``dist/patch-meta-linux.json``.

Usage: ``python scripts/generate_patch.py [windows|linux]`` (default windows).

When there is no usable previous release the script exits 0 and writes no
patch metadata, so the updater falls back to a full download.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.request import Request, urlopen

EXE_NAME = "yikou-light-food.exe"
CURRENT_EXE = Path("dist") / EXE_NAME
LINUX_TARBALL_NAME = "yikou-light-food-linux-x64.tar.gz"
CURRENT_LINUX_BINARY = Path("dist") / "yikou-light-food"


def _gh(*args: str) -> str:
    env = dict(os.environ)
    result = subprocess.run(
        ["gh", *args],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _previous_release(repository: str, current_tag: str, *, asset_name: str) -> tuple[str, str] | None:
    """Return ``(tag, download_url)`` of the previous release's asset."""
    releases = json.loads(_gh(
        "api", f"repos/{repository}/releases",
        "--jq", f'[.[] | select(.prerelease == false and .draft == false) | {{tag: .tag_name, assets: [.assets[] | select(.name == "{asset_name}") | .browser_download_url]}}]',
    ))
    for release in releases:
        tag = str(release.get("tag") or "")
        if tag == current_tag or not tag:
            continue
        assets = release.get("assets") or []
        if assets:
            return tag, str(assets[0])
    return None


def _download(url: str, destination: Path) -> None:
    request = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "yikou-light-food-release"})
    with urlopen(request, timeout=120) as response, destination.open("wb") as handle:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            handle.write(block)


def _extract_linux_binary(archive: Path, destination: Path) -> Path:
    """从上一版 tar.gz 中解出裸 onefile 二进制（即用户本地的基线文件）。"""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isreg() and Path(m.name).name == "yikou-light-food"]
        if not members:
            raise SystemExit(f"no yikou-light-food binary inside {archive}")
        tar.extract(members[0], destination, filter="data")
    binary = destination / "yikou-light-food"
    binary.chmod(0o755)
    return binary


def _generate_windows_patch(bsdiff4, work: Path, current_tag: str) -> dict | None:
    if not CURRENT_EXE.is_file():
        print("patch generation requires a built exe", file=sys.stderr)
        return None
    previous = _previous_release(os.environ.get("GITHUB_REPOSITORY", "zimu5683/yikou-light-food-desktop"),
                                 current_tag, asset_name=EXE_NAME)
    if previous is None:
        print("no previous release with a Windows exe; skipping patch", file=sys.stderr)
        return None
    previous_tag, download_url = previous

    previous_exe = work / f"{EXE_NAME}.{previous_tag}"
    patch_name = f"yikou-light-food-{previous_tag}-{current_tag}.patch"
    patch_path = Path("dist") / patch_name
    if not previous_exe.is_file():
        _download(download_url, previous_exe)
    from_sha256 = _sha256(previous_exe)
    bsdiff4.file_diff(str(previous_exe), str(CURRENT_EXE), str(patch_path))
    return {
        "name": patch_name,
        "from_sha256": from_sha256,
        "target_sha256": _sha256(CURRENT_EXE),
        "sha256": _sha256(patch_path),
    }


def _generate_linux_patch(bsdiff4, work: Path, current_tag: str) -> dict | None:
    if not CURRENT_LINUX_BINARY.is_file():
        print("patch generation requires a built Linux binary (dist/yikou-light-food)", file=sys.stderr)
        return None
    previous = _previous_release(os.environ.get("GITHUB_REPOSITORY", "zimu5683/yikou-light-food-desktop"),
                                 current_tag, asset_name=LINUX_TARBALL_NAME)
    if previous is None:
        print("no previous release with a Linux tarball; skipping patch", file=sys.stderr)
        return None
    previous_tag, download_url = previous

    previous_tarball = work / f"{LINUX_TARBALL_NAME}.{previous_tag}"
    patch_name = f"yikou-light-food-linux-x64-{previous_tag}-{current_tag}.patch"
    patch_path = Path("dist") / patch_name
    if not previous_tarball.is_file():
        _download(download_url, previous_tarball)
    previous_binary = _extract_linux_binary(previous_tarball, work / f"linux-{previous_tag}")
    from_sha256 = _sha256(previous_binary)
    bsdiff4.file_diff(str(previous_binary), str(CURRENT_LINUX_BINARY), str(patch_path))
    return {
        "name": patch_name,
        "platform": "linux",
        "from_sha256": from_sha256,
        "target_sha256": _sha256(CURRENT_LINUX_BINARY),
        "sha256": _sha256(patch_path),
    }


def main() -> int:
    import bsdiff4  # noqa: F401  — required only on the CI side

    platform = (sys.argv[1] if len(sys.argv) > 1 else "windows").lower()
    if platform not in {"windows", "linux"}:
        print("usage: generate_patch.py [windows|linux]", file=sys.stderr)
        return 2
    current_tag = os.environ.get("GITHUB_REF_NAME", "")
    if not current_tag.startswith("v"):
        print("patch generation requires a version tag", file=sys.stderr)
        return 2

    work = Path("dist") / ".patch-work"
    work.mkdir(parents=True, exist_ok=True)
    try:
        if platform == "windows":
            meta = _generate_windows_patch(bsdiff4, work, current_tag)
            meta_path = Path("dist") / "patch-meta.json"
        else:
            meta = _generate_linux_patch(bsdiff4, work, current_tag)
            meta_path = Path("dist") / "patch-meta-linux.json"
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    if meta is None:
        return 0
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
