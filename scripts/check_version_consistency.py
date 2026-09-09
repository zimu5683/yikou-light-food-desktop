"""Check app version and local release manifest consistency."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def main() -> int:
    init_text = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
    match = VERSION_RE.search(init_text)
    if not match:
        print("app/__init__.py 缺少 __version__", file=sys.stderr)
        return 1
    version = match.group(1)
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
        print(f"app 版本号不是严格 SemVer：{version}", file=sys.stderr)
        return 1

    manifest = ROOT / "latest.json"
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"latest.json 无法解析：{exc}", file=sys.stderr)
            return 1
        manifest_version = str(payload.get("version") or "").lstrip("vV")
        if manifest_version != version:
            print(
                f"latest.json ({manifest_version or '缺失'}) 与 app 版本 ({version}) 不一致；"
                "latest.json 只能由发布流水线生成，不应留在工作区。",
                file=sys.stderr,
            )
            return 1
    print(f"版本一致性检查通过：{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
