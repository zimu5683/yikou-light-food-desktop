"""Generate the portable-app latest.json from the completed GitHub release."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from urllib.request import Request, urlopen


REQUIRED = {
    "yikou-light-food.exe",
    "yikou-light-food.exe.sha256",
    "yikou-light-food-macos.zip",
    "yikou-light-food-macos.zip.sha256",
}


def release_payload(repository: str, tag: str) -> dict:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    url = f"https://api.github.com/repos/{repository}/releases/tags/{tag}"
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "yikou-light-food-release"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    repository = os.environ.get("GITHUB_REPOSITORY", "zimu5683/yikou-light-food-desktop")
    tag = os.environ.get("GITHUB_REF_NAME", "")
    if not tag.startswith("v"):
        print("latest.json is generated only for a version tag", file=sys.stderr)
        return 2
    payload: dict | None = None
    for attempt in range(30):
        try:
            candidate = release_payload(repository, tag)
            names = {str(item.get("name")) for item in candidate.get("assets", []) if isinstance(item, dict)}
            if REQUIRED <= names:
                payload = candidate
                break
        except Exception as exc:  # noqa: BLE001 - release assets are eventually consistent
            print(f"release query attempt {attempt + 1}: {exc}")
        time.sleep(10)
    if payload is None:
        raise RuntimeError("timed out waiting for all platform release assets")

    assets = []
    for item in payload.get("assets", []):
        name = str(item.get("name") or "")
        if name not in REQUIRED:
            continue
        assets.append({
            "name": name,
            "url": item.get("browser_download_url"),
            "size": item.get("size"),
            "sha256_url": next((other.get("browser_download_url") for other in payload.get("assets", [])
                                if other.get("name") == f"{name}.sha256"), None),
        })

    patches = _load_patches(payload)
    manifest = {
        "schema_version": 1,
        "version": str(payload.get("tag_name") or tag),
        "url": str(payload.get("html_url") or ""),
        "body": str(payload.get("body") or ""),
        "assets": assets,
    }
    if patches:
        manifest["patches"] = patches
    with open("latest.json", "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    subprocess.run(["gh", "release", "upload", tag, "latest.json", "--repo", repository, "--clobber"], check=True)
    return 0


def _load_patches(payload: dict) -> list[dict]:
    """Build the ``patches`` list from dist/patch-meta.json + release assets."""
    meta_path = os.path.join("dist", "patch-meta.json")
    if not os.path.isfile(meta_path):
        return []
    try:
        with open(meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        return []
    name = str(meta.get("name") or "")
    url_by_name = {
        str(item.get("name")): item.get("browser_download_url")
        for item in payload.get("assets", []) if isinstance(item, dict)
    }
    url = url_by_name.get(name)
    if not name or not url:
        return []
    return [{
        "name": name,
        "url": url,
        "sha256": str(meta.get("sha256") or "").strip().lower(),
        "from_sha256": str(meta.get("from_sha256") or "").strip().lower(),
        "target_sha256": str(meta.get("target_sha256") or "").strip().lower(),
    }]


if __name__ == "__main__":
    raise SystemExit(main())
