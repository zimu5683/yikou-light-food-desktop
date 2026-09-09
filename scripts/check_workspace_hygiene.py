"""Fail CI if diagnostic/business artifacts or obvious secrets are tracked."""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

FORBIDDEN_PATTERNS = (
    ".zcode/*",
    "*.jsonl",
    "localStorage.json",
    "*.har",
    "*.pcap",
    "*.xlsx",
    "*.xlsm",
    "*.xls",
    "*.log",
    "latest.json",
    "latest.json.sig",
    "*.bak",
    "*.pem",
    "*.key",
    "workspace-before-audit.patch",
)
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "dist", "build",
    ".pytest_cache", ".ruff_cache", "__pycache__",
}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
MAX_TEXT_BYTES = 2 * 1024 * 1024


def tracked_files() -> list[Path]:
    raw = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    return [Path(item.decode("utf-8")) for item in raw.split(b"\0") if item]


def working_tree_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            files.append(path.relative_to(root))
    return files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--working-tree", action="store_true",
        help="同时扫描未跟踪文件（本地运行用；CI 默认只扫描 Git 跟踪内容）",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    files = working_tree_files(root) if args.working_tree else tracked_files()
    mode = "working-tree" if args.working_tree else "tracked"
    failures: list[str] = []
    for path in files:
        value = path.as_posix()
        # .zcode 下的技能文件是经审计保留的版本控制内容；其余诊断数据禁止。
        if value.startswith(".zcode/") and not value.startswith(".zcode/skills/"):
            failures.append(f"禁止跟踪诊断/业务文件：{value}")
            continue
        if not value.startswith(".zcode/skills/"):
            for pattern in FORBIDDEN_PATTERNS:
                if fnmatch.fnmatch(value, pattern):
                    failures.append(f"禁止跟踪诊断/业务文件：{value}")
                    break
        try:
            if path.stat().st_size > MAX_TEXT_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                failures.append(f"疑似密钥/令牌被跟踪：{value}")
                break
    if failures:
        print(f"工作区卫生检查失败（{mode}）：", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"工作区卫生检查通过（{mode}）：未发现诊断数据或明显密钥。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
