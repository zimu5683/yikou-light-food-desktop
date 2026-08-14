"""Generate a bsdiff patch against the previous release's Windows exe.

Runs in the Windows CI job after ``PyInstaller`` has produced
``dist/yikou-light-food.exe`` and before the Release is published.  It:

1. finds the previous (non-prerelease, non-draft) release via ``gh api``;
2. downloads that release's ``yikou-light-food.exe``;
3. generates ``dist/yikou-light-food-{previous}-{current}.patch`` with bsdiff4;
4. writes ``dist/patch-meta.json`` describing the patch so
   ``generate_latest_manifest.py`` can expose it in ``latest.json``.

When there is no usable previous release the script exits 0 and writes no
patch metadata, so the updater falls back to a full download.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

EXE_NAME = "yikou-light-food.exe"
CURRENT_EXE = Path("dist") / EXE_NAME


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


def _previous_release(repository: str, current_tag: str) -> tuple[str, str] | None:
    """Return ``(tag, download_url)`` of the previous release's Windows exe."""
    releases = json.loads(_gh(
        "api", f"repos/{repository}/releases",
        "--jq", '[.[] | select(.prerelease == false and .draft == false) | {tag: .tag_name, assets: [.assets[] | select(.name == "yikou-light-food.exe") | .browser_download_url]}]',
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


def main() -> int:
    import bsdiff4  # noqa: F401  — required only on the CI side

    repository = os.environ.get("GITHUB_REPOSITORY", "zimu5683/yikou-light-food-desktop")
    current_tag = os.environ.get("GITHUB_REF_NAME", "")
    if not current_tag.startswith("v") or not CURRENT_EXE.is_file():
        print("patch generation requires a version tag and a built exe", file=sys.stderr)
        return 2

    previous = _previous_release(repository, current_tag)
    if previous is None:
        print("no previous release with a Windows exe; skipping patch", file=sys.stderr)
        return 0
    previous_tag, download_url = previous

    work = Path("dist") / ".patch-work"
    work.mkdir(exist_ok=True)
    previous_exe = work / f"{EXE_NAME}.{previous_tag}"
    patch_name = f"yikou-light-food-{previous_tag}-{current_tag}.patch"
    patch_path = Path("dist") / patch_name
    try:
        if not previous_exe.is_file():
            _download(download_url, previous_exe)
        from_sha256 = _sha256(previous_exe)
        bsdiff4.file_diff(str(previous_exe), str(CURRENT_EXE), str(patch_path))
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)

    meta = {
        "name": patch_name,
        "from_sha256": from_sha256,
        "target_sha256": _sha256(CURRENT_EXE),
        "sha256": _sha256(patch_path),
    }
    (Path("dist") / "patch-meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
